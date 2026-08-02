"""`tara` — one command to start the whole Taramandal stack, one Ctrl+C to stop it.

Replaces the six-terminal ritual (fleet script, two backends, two frontends,
optionally RViz) that this project used before Phase 1 of the finishing plan.
"""

from __future__ import annotations

import sys

import typer

from .config import load_config
from .doctor import run_checks
from .process import Supervisor
from .reset import check_stray_processes, reset_all
from .services import UnsupportedBackendError, build_services

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.command()
def up(
    n: int = typer.Option(None, "-n", "--num-drones", help="Fleet size. Defaults to tara.toml's [fleet].num_drones."),
    backend: str = typer.Option(None, "--backend", help='"px4" (real SITL, ~10 drones) or "sim" (Phase 2, not yet built).'),
    headless: bool = typer.Option(True, "--headless/--gui", help="Run PX4/Gazebo without the GUI window."),
    rviz: bool = typer.Option(None, "--rviz/--no-rviz", help="Also launch the ROS2 TF bridge + RViz."),
) -> None:
    """Start the fleet, both backends, and both web apps as one supervised unit."""
    config = load_config()
    num_drones = n if n is not None else config.num_drones
    backend_name = backend if backend is not None else config.backend
    rviz_enabled = rviz if rviz is not None else config.rviz

    try:
        services = build_services(
            config, num_drones=num_drones, backend=backend_name, headless=headless, rviz=rviz_enabled
        )
    except UnsupportedBackendError as exc:
        typer.secho(f"[tara] {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    typer.echo(
        f"[tara] starting {len(services)} services — backend={backend_name}, "
        f"drones={num_drones}, rviz={rviz_enabled}"
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
def reset() -> None:
    """Kill every stray fleet/backend/web process and clear Gazebo's lock files.

    Use this after a hard-killed `tara up` (e.g. the terminal itself was
    closed) left processes running outside the supervisor's control.
    """
    reset_all()


@app.command()
def status() -> None:
    """List any known Taramandal processes still running."""
    stray = check_stray_processes()
    if not stray:
        typer.echo("[tara] nothing running.")
        return
    typer.echo(f"[tara] {len(stray)} process(es) still running:")
    for line in stray:
        typer.echo(f"  {line}")


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
