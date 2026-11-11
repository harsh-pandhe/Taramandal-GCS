"""`tara` — one command to start the whole Taramandal stack, one Ctrl+C to stop it.

Replaces the six-terminal ritual (fleet script, two backends, two frontends,
optionally RViz) that this project used before Phase 1 of the finishing plan.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import typer
from tara_core.signing import (
    MANIFEST_SUFFIX,
    SignatureError,
    generate_private_key,
    key_fingerprint,
    private_key_from_pem,
    private_key_to_pem,
    public_key_to_b64,
    sign_show,
)

from .config import load_config
from .doctor import run_checks
from .process import Supervisor
from .reset import reset_all
from .services import UnsupportedBackendError, build_services
from .status import collect_status, to_json, to_text

app = typer.Typer(add_completion=False, no_args_is_help=True)


def _check_jitter_ms(jitter_ms: float, backend_name: str) -> None:
    """Refuse a nonsense --jitter-ms before anything is spawned, and say when it does nothing (issue #207)."""
    # `not (x >= 0)` also catches NaN, which compares false with everything.
    if not (math.isfinite(jitter_ms) and jitter_ms >= 0.0):
        raise typer.BadParameter(f"must be a finite number of milliseconds, 0 or more, got {jitter_ms}", param_hint="--jitter-ms")
    if jitter_ms == 0.0:
        return
    if backend_name != "sim":
        typer.secho(
            f"[tara] --jitter-ms {jitter_ms} is ignored on --backend {backend_name}: only tara_sim models TDMA slot "
            "jitter (use --backend sim).",
            fg=typer.colors.YELLOW,
            err=True,
        )


@app.command()
def up(
    n: int = typer.Option(None, "-n", "--num-drones", help="Fleet size. Defaults to tara.toml's [fleet].num_drones."),
    backend: str = typer.Option(None, "--backend", help='"px4" (real SITL, ~10 drones) or "sim" (Phase 2, not yet built).'),
    headless: bool = typer.Option(True, "--headless/--gui", help="Run PX4/Gazebo without the GUI window."),
    rviz: bool = typer.Option(None, "--rviz/--no-rviz", help="Also launch the ROS2 TF bridge + RViz."),
    allow_unsigned: bool = typer.Option(
        False, "--allow-unsigned",
        help="Let the GCS accept trajectory files without a signature manifest (legacy shows, local dev).",
    ),
    seed: int = typer.Option(
        None, "--seed", help="Seed tara_sim's physics/RF-channel randomness for reproducible runs (backend=sim only).",
    ),
    packet_loss: float = typer.Option(
        0.0, "--packet-loss",
        help="Base RF packet-error rate for tara_sim's channel model, e.g. 0.05 for 5 percent (backend=sim only; issue #62).",
    ),
    packet_loss_per_100m: float = typer.Option(
        0.0, "--packet-loss-per-100m",
        help="Additional packet-error rate per 100m of distance from each drone's pad (backend=sim only; issue #62).",
    ),
    jitter_ms: float = typer.Option(
        0.0, "--jitter-ms",
        help="TDMA slot jitter for tara_sim's channel model: standard deviation of each drone's send-time error, "
        "in ms; a send past its slot's guard time collides with the neighbouring slot (backend=sim only; issue #207).",
    ),
) -> None:
    """Start the fleet, both backends, and both web apps as one supervised unit."""
    config = load_config()
    num_drones = n if n is not None else config.num_drones
    backend_name = backend if backend is not None else config.backend
    rviz_enabled = rviz if rviz is not None else config.rviz

    # Fail here, before anything is spawned, rather than as one interleaved
    # line in the fleet service's log while the backends start up regardless.
    if backend_name == "px4" and not Path(config.px4_autopilot_dir).is_dir():
        typer.secho(
            f"[tara] PX4-Autopilot not found at {config.px4_autopilot_dir}.\n"
            "[tara] Set PX4_AUTOPILOT_DIR to your checkout (or [px4].autopilot_dir in "
            "tara.toml), or run with --backend sim.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)

    _check_jitter_ms(jitter_ms, backend_name)

    try:
        services = build_services(
            config, num_drones=num_drones, backend=backend_name, headless=headless, rviz=rviz_enabled,
            allow_unsigned=allow_unsigned, sim_seed=seed, sim_packet_loss=packet_loss,
            sim_packet_loss_per_100m=packet_loss_per_100m, sim_jitter_ms=jitter_ms,
        )
    except UnsupportedBackendError as exc:
        typer.secho(f"[tara] {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    typer.echo(
        f"[tara] starting {len(services)} services — backend={backend_name}, "
        f"drones={num_drones}, rviz={rviz_enabled}"
    )
    if allow_unsigned:
        typer.secho(
            "[tara] --allow-unsigned: the GCS will accept trajectory files that carry no signature.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    typer.echo(
        f"[tara]   GCS dashboard    http://localhost:{config.gcs_web_port}\n"
        f"[tara]   Studio           http://localhost:{config.studio_web_port}\n"
        f"[tara]   GCS API docs     http://localhost:{config.gcs_backend_port}/docs\n"
        f"[tara]   Studio API docs  http://localhost:{config.studio_backend_port}/docs"
    )

    supervisor = Supervisor()
    # Stagger starts so each service's dependency is already listening: the
    # fleet needs ~10s before Gazebo/PX4 are up, the backends need a moment to
    # bind before the web apps that call them on load, and Studio's frontend
    # depends on both APIs.
    stagger = {
        "fleet": 0.0,
        "gcs-api": 12.0 if backend_name == "px4" else 0.0,
        "studio-api": 0.0,
        "gcs-web": 2.0,
        "studio-web": 0.0,
        "rviz": 3.0,
    }
    for service in services:
        supervisor.add(service, delay_before=stagger.get(service.name, 0.0))

    exit_code = supervisor.run_until_exit_or_signal()
    typer.echo("[tara] all services stopped.")
    raise typer.Exit(exit_code)


@app.command()
def keygen(
    out: Path = typer.Argument(..., help="Where to write the new private key (PEM). Keep it off shared drives and out of git."),
    signer: str = typer.Option(..., "--signer", help="Who this key belongs to, as the GCS will report it."),
) -> None:
    """Create an Ed25519 show-signing key and print the tara.toml line that trusts it."""
    if out.exists():
        typer.secho(f"[tara] {out} already exists — refusing to overwrite a key.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    key = generate_private_key()
    out.parent.mkdir(parents=True, exist_ok=True)
    # Create with owner-only permissions from the start, rather than chmod
    # after writing, so the key is never briefly world-readable.
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(private_key_to_pem(key))

    public_key = key.public_key()
    typer.echo(f"[tara] wrote private key {out} (key id {key_fingerprint(public_key)})")
    typer.echo("[tara] add this to [security].trusted_keys in tara.toml on every GCS that should accept it:")
    typer.echo(f'    {{ signer = "{signer}", public_key = "{public_key_to_b64(public_key)}" }},')


@app.command()
def sign(
    show: Path = typer.Argument(..., exists=True, dir_okay=False, help="The .json/.csv/.skyc show file to sign."),
    key: Path = typer.Option(..., "--key", exists=True, dir_okay=False, help="Private key from `tara keygen`."),
) -> None:
    """Write SHOW.sig.json, the signature manifest the GCS needs to accept SHOW."""
    # Like ssh with a private key: refuse one other users can read (issue #64).
    # `tara keygen` creates keys 0600; this catches one copied or chmod-ed since.
    mode = key.stat().st_mode & 0o777
    if os.name == "posix" and mode & 0o077:
        typer.secho(
            f"[tara] {key} has permissions {mode:o}, so other users can read this signing key. "
            f"Run `chmod 600 {key}` and try again.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(1)
    try:
        private_key = private_key_from_pem(key.read_bytes())
    except SignatureError as exc:
        typer.secho(f"[tara] {key}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    manifest_path = show.with_name(show.name + MANIFEST_SUFFIX)
    manifest_path.write_bytes(sign_show(show.read_bytes(), show.name, private_key))
    typer.echo(f"[tara] signed {show} with key {key_fingerprint(private_key.public_key())} -> {manifest_path}")
    typer.echo("[tara] upload both files together; changing either one invalidates the signature.")


def _violation_lines(report: dict, label: str) -> list[str]:
    """One line per failing check in a Studio feasibility report, with its first offender."""
    lines = []
    for category, items in report.get("violations", {}).items():
        if items:
            first = ", ".join(f"{k}={round(v, 3) if isinstance(v, float) else v}" for k, v in items[0].items())
            lines.append(f"{label}: {category}: {len(items)} violation(s), first: {first}")
    return lines or [f"{label}: {report.get('summary', 'failed feasibility certification')}"]


def _feasibility_errors(show_obj, missions, origins) -> list[str]:
    """Run Studio's export gate, as Studio runs it, on the show and on the bytes about to be written.

    Studio refuses a flight export unless ``certify_feasibility_report`` passes on the show as
    given and again on the compiled file decoded back, because centimetre rounding can push a
    marginal show over a velocity, acceleration or jerk limit (issues #157, #175). This does the
    same: the second pass decodes the exact payloads and shifts them back to the fleet frame.
    Fails closed: if the gate itself cannot run, that is an error, not a pass.
    """
    from tara_core import Show, decompile_drone_mission
    from tara_core.validate import altitude_errors

    try:
        from tara_studio.validator import certify_feasibility_report

        errors: list[str] = []
        certified, report = certify_feasibility_report(show_obj)
        if not certified:
            return _violation_lines(report, "as designed")

        decoded = {}
        for drone_id, mission in missions.items():
            ox, oy = origins[drone_id]
            _, waypoints = decompile_drone_mission(mission.payload)
            decoded[drone_id] = [{**wp, "x": wp["x"] + ox, "y": wp["y"] + oy} for wp in waypoints]
        compiled_show = Show.from_any(decoded)
        errors += [f"as compiled (after centimetre rounding): {e}" for e in altitude_errors(compiled_show)]
        certified, report = certify_feasibility_report(compiled_show)
        if not certified:
            errors += _violation_lines(report, "as compiled (after centimetre rounding)")
        return errors
    except Exception as exc:
        return [f"could not run the feasibility gate, so the show is not certified: {exc!r}"]


@app.command()
def compile(
    show: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        help="The show trajectory file (.json/.csv/.skyc) to compile.",
    ),
    output_dir: Path = typer.Option(
        Path("./dist"),
        "--output-dir",
        "-o",
        help="Directory where .tarm files and manifest.json will be written.",
    ),
    validate: bool = typer.Option(
        True,
        "--validate/--no-validate",
        help=(
            "Run the safety gates before writing anything: altitude, separation and Studio's full "
            "feasibility certification (velocity, acceleration, yaw rate, jerk). --no-validate skips "
            "these gates only; input that cannot be compiled faithfully is always refused."
        ),
    ),
) -> None:
    """Compile canonical show trajectories into deterministic binary .tarm payloads (issue #56).

    Each drone's file is in its own launch-pad frame and byte-identical to the one the GCS
    uploads for the same show. A show that Studio would refuse to export is refused here too,
    with the reasons, and nothing is written."""
    from tara_core import Show
    from tara_core.compiler import compile_input_errors, compile_show, write_compiled_show
    from tara_core.validate import altitude_errors, collision_errors
    from tara_gcs.drone_manager import _pad_offset

    try:
        suffix = show.suffix.lower()
        if suffix in (".csv", ".skyc"):
            from tara_gcs.trajectory_parser import parse_trajectory_file

            content = show.read_text(encoding="utf-8") if suffix == ".csv" else show.read_bytes()
            parsed = parse_trajectory_file(content, show.name)
            show_obj = Show.from_any(parsed)
        else:
            show_obj = Show.from_any(json.loads(show.read_text(encoding="utf-8")))
    except Exception as exc:
        typer.secho(f"[tara] failed to load show {show}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    input_errors = compile_input_errors(show_obj)
    if input_errors:
        for err in input_errors:
            typer.secho(f"[tara] invalid show: {err}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    if validate:
        safety_errors = altitude_errors(show_obj) + collision_errors(show_obj)
        if safety_errors:
            for err in safety_errors:
                typer.secho(f"[tara] safety error: {err}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)

    try:
        # Compile in each drone's own pad frame, exactly as the GCS does when it
        # uploads a show (DroneManager.upload_show): the file header promises
        # drone-local NED, so a fleet-frame file loaded onto a drone that is not
        # at the origin pad would command a sideways move at t = 0. The pad grid
        # is the GCS's own, imported rather than copied so the two cannot drift.
        # Slot N is compiled for drone N, which is what the GCS assigns when the
        # whole fleet is healthy; offline there is no health to remap around.
        origins = {slot: _pad_offset(slot) for slot in show_obj.drones}
        missions, manifest = compile_show(show_obj, origins=origins)
    except Exception as exc:
        typer.secho(f"[tara] compilation failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    if validate:
        gate_errors = _feasibility_errors(show_obj, missions, origins)
        if gate_errors:
            for err in gate_errors:
                typer.secho(f"[tara] refused: {err}", fg=typer.colors.RED, err=True)
            typer.secho(
                "[tara] nothing was written. Fix the show in Studio and compile again.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(1)

    try:
        write_compiled_show(missions, manifest, output_dir)
    except OSError as exc:
        typer.secho(f"[tara] could not write to {output_dir}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    typer.echo(f"[tara] compiled show (id {manifest.show_id}) with {manifest.drone_count} drones into {output_dir}")
    typer.echo(f"[tara] wrote manifest.json (SHA-256: {manifest.manifest_sha256})")
    typer.echo(f"[tara] total payload: {manifest.total_binary_bytes} bytes across {manifest.total_waypoint_count} waypoints")
    for did in sorted(manifest.drones.keys()):
        entry = manifest.drones[did]
        typer.echo(f"[tara]   drone {did}: {entry.waypoint_count} wps, {entry.size_bytes}B -> {entry.file_name} ({entry.sha256[:12]}...)")



@app.command()
def reset() -> None:
    """Kill every stray fleet/backend/web process and clear Gazebo's lock files.

    Use this after a hard-killed `tara up` (e.g. the terminal itself was
    closed) left processes running outside the supervisor's control.
    """
    reset_all()


@app.command()
def status(
    as_json: bool = typer.Option(False, "--json", help="Emit the report as JSON for scripts and CI."),
) -> None:
    """Report whether the stack is up, and the fleet's connected/armed counts.

    Exits 1 if processes are running but the GCS API can't be reached — a
    half-dead stack. An idle machine with nothing running exits 0, since
    "down" is not an error unless the stack was supposed to be up.
    """
    config = load_config()
    snapshot = collect_status(config)

    typer.echo(to_json(snapshot) if as_json else to_text(snapshot))

    if snapshot.processes_running and not snapshot.api_reachable:
        raise typer.Exit(1)


@app.command()
def doctor() -> None:
    """Check that PX4, Gazebo, Node, and Python dependencies are all in place."""
    config = load_config()
    results = run_checks(config)

    all_ok = True
    for r in results:
        mark = "OK  " if r.ok else "FAIL"
        color = typer.colors.GREEN if r.ok else typer.colors.RED
        typer.secho(f"[{mark}] {r.name}: {r.detail}", fg=color)
        all_ok = all_ok and r.ok

    if not all_ok:
        typer.secho("\n[tara] one or more checks failed — see above.", fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.secho("\n[tara] all checks passed.", fg=typer.colors.GREEN)


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main() or 0)
