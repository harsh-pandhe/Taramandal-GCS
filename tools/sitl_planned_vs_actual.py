#!/usr/bin/env python3
"""Planned vs actual for a Studio-built show flown on PX4 SITL + Gazebo (issue #172).

One ``run`` flies one show through the real stack that ``./tara up --backend
px4 -n N`` starts, and records what Studio predicted next to what Gazebo saw:

1. Build the show: a Studio sequencer stand-in (``--drones N``), or a choreo
   JSON exported from Studio (``--show path``).
2. Studio ``/api/validate`` must certify it (jerk included).
3. Studio ``/api/export`` signs it; the export must not be ``preview_only``.
4. GCS: ``/api/upload-trajectory`` (signature must verify), ``/api/upload-mission``,
   the two-step pre-arm latch, ``/api/launch``, wait for FORMATION_HOLD, then
   ``/api/start-trajectory`` with ``execution="streamed"``.
5. Wait for SHOW_COMPLETE (or an abort), land, wait for DISARMED.

Ground truth is Gazebo's own model poses (``/world/<world>/dynamic_pose/info``,
about 50 Hz), read with the ``gz`` CLI. It does not go through PX4 or the GCS,
so a GCS frame or pad-offset error shows up instead of cancelling out. The
GCS's own view (its telemetry CSV export) is recorded too, so the two can be
compared.

Frames: the show and the GCS use the fleet frame, x = north, y = east, z down
(altitude = -z). Gazebo's world is ENU, so fleet x = gz y and fleet y = gz x.
Altitude is gz z minus each model's resting z before arming.

``summarize`` turns several run JSONs into a Markdown table.

Run it inside the container that runs the stack (the ``gz`` CLI must reach the
Gazebo partition). It uses only the standard library, so any Python 3.10+ works.

Usage:
    python3 tools/sitl_planned_vs_actual.py run --drones 3 --out /scratch/runs --label p1
    python3 tools/sitl_planned_vs_actual.py summarize /scratch/runs/*.json
"""

from __future__ import annotations

import argparse
import bisect
import csv
import io
import json
import math
import os
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Fleet states in which the GCS has taken the fleet out of the show.
EMERGENCY_STATES = {"LANDING", "RTL", "ESTOP"}

#: Gazebo publishes poses at ~50 Hz. A gap this long in sim time, or in
#: messages, while wall time keeps going is a frozen or diverged sim; such a
#: run is discarded, not counted (Tab 2's rule for #171/#172).
SIM_STALL_S = 2.0


# --- HTTP -----------------------------------------------------------------

class Api:
    """Minimal JSON client for the GCS and Studio backends."""

    def __init__(self, base: str, token: str | None = None):
        self.base = base.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, body=None, *, raw: bytes | None = None,
             content_type: str | None = None, timeout: float = 120.0):
        headers = {}
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            payload = e.read()
            status = e.code
        try:
            return status, json.loads(payload or b"null")
        except json.JSONDecodeError:
            return status, payload.decode(errors="replace")

    def must(self, method: str, path: str, body=None, **kw):
        status, out = self.call(method, path, body, **kw)
        if status != 200:
            raise RunError(f"{method} {path} -> {status}: {out}")
        return out


class RunError(RuntimeError):
    """The run could not continue. The message goes into the result."""


def multipart(fields: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    """Encode ``{name: (filename, content, content_type)}`` as multipart/form-data."""
    boundary = uuid.uuid4().hex
    buf = io.BytesIO()
    for name, (filename, content, ctype) in fields.items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode())
        buf.write(f"Content-Type: {ctype}\r\n\r\n".encode())
        buf.write(content)
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


# --- Show -----------------------------------------------------------------

STAND_IN_JS = """
import(process.argv[1]).then(({ buildSequencedShow, makeScene }) => {
  const scenes = [makeScene({ name: 'Circle', shapeType: 'parametric', shapePayload: 'circle',
                              holdDurationSeconds: 6, transitionDurationSeconds: 4 })];
  const show = buildSequencedShow(scenes, Number(process.argv[2]), Number(process.argv[3]));
  process.stdout.write(JSON.stringify({ drones: show.drones }));
});
"""


def build_stand_in(num_drones: int, rate_hz: float) -> dict:
    """The #209 stand-in: one circle scene from Studio's own sequencer."""
    sequencer = REPO_ROOT / "packages" / "tara-studio-web" / "src" / "engine" / "sequencer.js"
    proc = subprocess.run(
        ["node", "-e", STAND_IN_JS, sequencer.as_uri(), str(num_drones), str(rate_hz)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)


_TIMES: dict[int, list[float]] = {}


def interpolate(wps: list[dict], t: float) -> tuple[float, float, float]:
    """Linear position at time t, clamped at the ends; same rule as
    tara_core.geometry.interpolate_waypoint, which the GCS streams from.
    Returns fleet-frame (x north, y east, altitude up)."""
    if t <= wps[0]["time"]:
        w = wps[0]
        return w["x"], w["y"], -w["z"]
    if t >= wps[-1]["time"]:
        w = wps[-1]
        return w["x"], w["y"], -w["z"]
    times = _TIMES.get(id(wps))
    if times is None:
        times = _TIMES[id(wps)] = [w["time"] for w in wps]
    i = bisect.bisect_left(times, t)
    a, b = wps[i - 1], wps[i]
    span = b["time"] - a["time"]
    r = (t - a["time"]) / span if span > 0 else 0.0
    return (a["x"] + r * (b["x"] - a["x"]),
            a["y"] + r * (b["y"] - a["y"]),
            -(a["z"] + r * (b["z"] - a["z"])))


# --- Gazebo ground truth --------------------------------------------------

class GazeboRecorder:
    """Reads model poses from ``gz topic -e --json-output`` on a thread.

    Each sample is ``(wall_s, sim_s, {drone_id: (gz_x, gz_y, gz_z)})``. Only
    the ``x500_<i>`` models are kept.
    """

    def __init__(self, world: str, num_drones: int):
        self.topic = f"/world/{world}/dynamic_pose/info"
        self.names = {f"x500_{i}": i for i in range(num_drones)}
        self.samples: list[tuple[float, float, dict[int, tuple[float, float, float]]]] = []
        self.errors: list[str] = []
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        env = dict(os.environ)
        env.setdefault("GZ_PARTITION", "taramandal")
        env.setdefault("GZ_IP", "127.0.0.1")
        self._proc = subprocess.Popen(
            ["gz", "topic", "-e", "-t", self.topic, "--json-output"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env, bufsize=1,
        )
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            wall = time.time()
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as e:
                if len(self.errors) < 5:
                    self.errors.append(f"bad gz json: {e}")
                continue
            stamp = msg.get("header", {}).get("stamp", {})
            sim = float(stamp.get("sec", 0)) + float(stamp.get("nsec", 0)) / 1e9
            poses = {}
            for p in msg.get("pose", []):
                i = self.names.get(p.get("name"))
                if i is not None:
                    pos = p.get("position", {})
                    poses[i] = (float(pos.get("x", 0.0)), float(pos.get("y", 0.0)), float(pos.get("z", 0.0)))
            if poses:
                self.samples.append((wall, sim, poses))

    def wait_for_all(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.samples and len(self.samples[-1][2]) == len(self.names):
                return True
            time.sleep(0.2)
        return False

    def stop(self) -> None:
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()


def to_fleet(gz: tuple[float, float, float], rest_z: float) -> tuple[float, float, float]:
    """Gazebo ENU world -> fleet frame (x north, y east, altitude up)."""
    return gz[1], gz[0], gz[2] - rest_z


# --- The run --------------------------------------------------------------

def wait_state(gcs: Api, targets: set[str], timeout: float, stop_on: frozenset[str] | set[str] = frozenset()) -> str:
    deadline = time.time() + timeout
    state = "?"
    while time.time() < deadline:
        status, body = gcs.call("GET", "/api/status", timeout=10)
        if status == 200:
            state = body["fleet_state"]
            if state in targets or state in stop_on:
                return state
        time.sleep(0.5)
    return state


def git_commit() -> str | None:
    """HEAD of the checkout, or None (a worktree mounted into a container
    often cannot resolve its .git link; pass --commit there)."""
    try:
        out = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def run(args) -> dict:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    label = args.label or time.strftime("%Y%m%d-%H%M%S")
    result: dict = {"label": label, "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "commit": args.commit or git_commit(), "gcs": args.gcs, "studio": args.studio,
                    "steps": {}, "verdict": None}
    steps = result["steps"]
    recorder = None
    try:
        # 1. Show
        if args.show:
            choreo = json.loads(Path(args.show).read_text())
            result["show_source"] = str(args.show)
        else:
            choreo = build_stand_in(args.drones, args.rate_hz)
            result["show_source"] = f"stand-in: Studio sequencer circle, {args.drones} drones, {args.rate_hz} Hz (as in #209)"
        choreo = {"drones": choreo["drones"]}
        drones = {int(d["id"]): d["waypoints"] for d in choreo["drones"]}
        n = len(drones)
        planned_duration = max(w[-1]["time"] for w in drones.values())
        result["num_drones"] = n
        result["planned"] = {"duration_s": planned_duration}

        # 2. Studio certification
        studio = Api(args.studio)
        report = studio.must("POST", "/api/validate", choreo, timeout=300)
        result["planned"].update({
            "studio_status": report.get("status"),
            "feasibility_certified": report.get("feasibility_certified"),
            "min_separation_m": report["metrics"]["min_distance_m"],
            "max_velocity_m_s": report["metrics"].get("max_velocity_m_s"),
            "max_jerk_m_s3": report["metrics"].get("max_jerk_m_s3"),
            "max_altitude_m": report["metrics"].get("max_altitude_m"),
            "limits": report.get("limits"),
        })
        if not report.get("feasibility_certified"):
            raise RunError(f"Studio did not certify the show: {report.get('summary')}")
        steps["validate"] = "certified"

        # 3. Signed export
        exported = studio.must("POST", "/api/export", {"choreo": choreo, "format": "json", "sign": True}, timeout=300)
        if exported.get("preview_only"):
            raise RunError("Studio export came back preview_only; the GCS would refuse it")
        if not exported.get("signature"):
            raise RunError("Studio export is unsigned; set STUDIO_SIGNING_KEY for the Studio backend")
        (out_dir / f"{label}.show.json").write_text(exported["content"])
        (out_dir / f"{label}.show.json.sig.json").write_text(exported["signature"]["content"])
        steps["export"] = "signed"

        # 4. GCS
        gcs = Api(args.gcs)
        login = gcs.must("POST", "/api/auth/login", {"operator": args.operator, "passphrase": args.passphrase})
        gcs.token = login.get("token")
        status = gcs.must("GET", "/api/status")
        result["gcs_status_before"] = status
        deadline = time.time() + args.connect_timeout
        while status["connected"] < n and time.time() < deadline:
            time.sleep(1)
            status = gcs.must("GET", "/api/status")
        if status["connected"] < n:
            raise RunError(f"only {status['connected']}/{n} drones connected to the GCS")
        if status["fleet_state"] != "STAGED":
            raise RunError(f"fleet is {status['fleet_state']}, not STAGED; restart the stack between runs")

        body, ctype = multipart({
            "file": (exported["filename"], exported["content"].encode(), "application/json"),
            "signature": (exported["filename"] + ".sig.json", exported["signature"]["content"].encode(), "application/json"),
        })
        up = gcs.must("POST", "/api/upload-trajectory", raw=body, content_type=ctype)
        if not up.get("signature", {}).get("verified"):
            raise RunError(f"GCS did not verify the signature: {up.get('signature')}")
        steps["upload_trajectory"] = {"signature": up["signature"], "geofence": up.get("geofence")}

        gcs.must("POST", "/api/upload-mission", timeout=300)
        mstatus = gcs.must("GET", "/api/mission-status")
        steps["upload_mission"] = {"all_verified": mstatus.get("all_verified"),
                                   "verified_count": mstatus.get("verified_count"),
                                   "launch_gate": mstatus.get("launch_gate", {}).get("reason") or "ok"}
        if not mstatus.get("launch_gate", {}).get("ok"):
            raise RunError(f"launch gate closed after mission upload: {mstatus.get('launch_gate')}")
        # A drone the GCS found unhealthy is dropped and its show slot handed to
        # another drone, which then flies (and lands) somewhere the plan did not
        # put it. Such a run measures the rig, not the show: refuse to score it.
        if mstatus.get("verified_count") != n:
            raise RunError(
                f"only {mstatus.get('verified_count')} of {n} drones took the mission "
                f"(excluded: {(mstatus.get('excluded') or mstatus.get('staged', {}).get('excluded'))}); "
                f"the GCS re-mapped the show - rerun once every drone is healthy"
            )

        # Ground truth starts before arming, to learn each model's resting z.
        recorder = GazeboRecorder(args.world, n)
        recorder.start()
        if not recorder.wait_for_all(20):
            raise RunError(f"no Gazebo poses for all {n} drones on {recorder.topic}")
        time.sleep(1.0)
        rest_z = {i: statistics.median(s[2][i][2] for s in recorder.samples if i in s[2]) for i in range(n)}

        pre = gcs.must("GET", "/api/preflight-check")
        steps["preflight"] = {"launch_permitted": pre.get("launch_permitted"), "blocking_reasons": pre.get("blocking_reasons")}
        latch = gcs.must("POST", "/api/preflight/latch/request", {})
        gcs.must("POST", "/api/preflight/latch/confirm", {"token": latch["token"]})
        t_launch = time.time()
        launch = gcs.must("POST", "/api/launch")
        steps["launch"] = {"drone_ids": launch.get("drone_ids"), "excluded": launch.get("excluded_drone_ids")}

        state = wait_state(gcs, {"FORMATION_HOLD"}, args.launch_timeout, stop_on=EMERGENCY_STATES | {"STAGED", "DISARMED"})
        steps["formation_hold_after_s"] = round(time.time() - t_launch, 1)
        if state != "FORMATION_HOLD":
            raise RunError(f"fleet reached {state}, not FORMATION_HOLD, within {args.launch_timeout}s of launch")
        time.sleep(args.hold_s)

        # 5. Streamed start
        t_start_req = time.time()
        started = gcs.must("POST", "/api/start-trajectory", {"mode": "LOCAL", "execution": "streamed"})
        t_start_ack = time.time()
        # Seconds the show's t=0 is behind the request: the streamed start first
        # flies each drone from the formation hold to the show's first waypoint
        # (issue #172 F3). Absent on a GCS without that leg.
        bridge_s = float(started.get("bridge_s") or 0.0)
        # The playback clock starts when the GCS's loop task first runs, which
        # is inside the request; take the midpoint (the two are ~ms apart).
        t0 = (t_start_req + t_start_ack) / 2
        steps["start"] = {"execution": "streamed", "mode": "LOCAL", "ack_latency_s": round(t_start_ack - t_start_req, 3),
                          "bridge_s": bridge_s}

        aborted_seen: set[int] = set()
        end_state = None
        deadline = t0 + bridge_s + planned_duration + args.show_slack_s
        while time.time() < deadline:
            s, st = gcs.call("GET", "/api/status", timeout=10)
            if s == 200:
                aborted_seen.update(st.get("aborted") or [])
                if st["fleet_state"] in {"SHOW_COMPLETE"} | EMERGENCY_STATES | {"DISARMED", "FORMATION_HOLD"}:
                    end_state = st["fleet_state"]
                    break
            time.sleep(0.5)
        fleet = gcs.must("GET", "/api/fleet-state")
        history = fleet.get("history", [])
        complete = next((h for h in history if h.get("to") == "SHOW_COMPLETE" and h.get("ts", 0) >= t_start_req), None)
        result["actual"] = {"end_state": end_state or f"timeout ({fleet.get('state')})"}
        if complete:
            # The GCS loop declares completion 1.0 s after the last waypoint.
            result["actual"]["show_complete_after_s"] = round(complete["ts"] - t0, 2)
            result["actual"]["playback_duration_s"] = round(complete["ts"] - t0 - bridge_s - 1.0, 2)
            result["actual"]["bridge_s"] = bridge_s

        # The GCS's own recorded view of the show (its 0.5 s telemetry log).
        s, csv_text = gcs.call("GET", f"/api/export-telemetry?filename=sitl172_{label}.csv")
        gcs_rows = []
        if s == 200 and isinstance(csv_text, str):
            (out_dir / f"{label}.gcs_telemetry.csv").write_text(csv_text)
            gcs_rows = list(csv.DictReader(io.StringIO(csv_text)))

        # Land and wait for everyone to disarm.
        land_state = wait_state(gcs, {"DISARMED"}, args.disarm_wait_s)
        if land_state != "DISARMED":
            s, out = gcs.call("POST", "/api/land")
            steps["land"] = {"status": s, "detail": out if s != 200 else "ok"}
            land_state = wait_state(gcs, {"DISARMED"}, args.land_timeout)
        result["actual"]["final_state"] = land_state
        time.sleep(1.0)
        recorder.stop()

        fleet = gcs.must("GET", "/api/fleet-state")
        history = [h for h in fleet.get("history", []) if h.get("ts", 0) >= t_launch - 1]
        result["fleet_history"] = [{k: h.get(k) for k in ("time", "from", "to", "trigger", "operator")} for h in history]
        result["actual"]["aborted_drones"] = sorted(aborted_seen)
        result["actual"]["emergency_transitions"] = [
            h for h in result["fleet_history"]
            if h["to"] in EMERGENCY_STATES and not (h["to"] == "LANDING" and h["trigger"] == "POST /api/land")
        ]

        analyse(result, recorder, drones, rest_z, t0, planned_duration, gcs_rows, out_dir, label, bridge_s)
    except RunError as e:
        result["error"] = str(e)
    except Exception as e:  # keep the partial record; the traceback goes to stderr
        result["error"] = f"{type(e).__name__}: {e}"
        import traceback
        traceback.print_exc()
    finally:
        if recorder:
            recorder.stop()
            result["gazebo_reader_errors"] = recorder.errors

    result["verdict"] = verdict(result)
    path = out_dir / f"{label}.json"
    path.write_text(json.dumps(result, indent=2))
    print(json.dumps({k: result.get(k) for k in ("label", "verdict", "error", "planned", "actual")}, indent=2))
    print(f"wrote {path}")
    return result


def min_jerk(tau: float) -> float:
    """Position fraction at normalised time tau (0..1) of the GCS's start
    transition leg (tara_gcs.start_bridge): zero velocity at both ends."""
    return tau**3 * (10.0 - 15.0 * tau + 6.0 * tau * tau)


def make_planner(drones, bridge_s: float, holds: dict[int, tuple[float, float, float]]):
    """``planned(i, t)``: where drone i should be ``t`` s after the start request.

    With no leg (``bridge_s == 0``) that is the show itself. With one, it is the
    GCS's minimum-jerk leg from where the drone hovered (``holds``, measured from
    Gazebo just before the start) to the show's first waypoint, then the show
    delayed by ``bridge_s``. Fleet frame, altitude up.
    """
    if bridge_s <= 0.0:
        return lambda i, t: interpolate(drones[i], t)

    def planned(i: int, t: float) -> tuple[float, float, float]:
        if t >= bridge_s or i not in holds:
            return interpolate(drones[i], max(0.0, t - bridge_s))
        f = min_jerk(max(0.0, t) / bridge_s)
        a, b = holds[i], interpolate(drones[i], 0.0)
        return tuple(x + f * (y - x) for x, y in zip(a, b))

    return planned


def hold_positions(recorder, rest_z, t0: float, n: int) -> dict[int, tuple[float, float, float]]:
    """Each drone's Gazebo position in the second before the start request."""
    window = [s for s in recorder.samples if t0 - 1.0 <= s[0] <= t0 + 0.05 and len(s[2]) == n]
    return {
        i: tuple(statistics.median(to_fleet(s[2][i], rest_z[i])[a] for s in window) for a in range(3))
        for i in range(n)
    } if window else {}


def start_descent_rate(show, rest_z, n: int, t0: float, window_s: float) -> dict[str, float]:
    """Fastest downward speed (m/s, over >= 0.25 s) of each drone's Gazebo
    altitude in the first ``window_s`` after the start request."""
    out = {}
    for i in range(n):
        alts = [(w - t0, to_fleet(poses[i], rest_z[i])[2]) for w, _sim, poses in show if w - t0 <= window_s]
        worst, k = 0.0, 0
        for j, (t, z) in enumerate(alts):
            while alts[k][0] < t - 0.25:
                k += 1
            if t - alts[k][0] >= 0.2:
                worst = max(worst, (alts[k][1] - z) / (t - alts[k][0]))
        out[str(i)] = round(worst, 2)
    return out


def analyse(result, recorder, drones, rest_z, t0, duration, gcs_rows, out_dir: Path, label: str, bridge_s: float = 0.0) -> None:
    """Planned vs Gazebo: separation, tracking error, real-time factor, stalls.

    ``bridge_s`` is the start transition leg the GCS flew before the show (see
    make_planner); ``duration`` is the show's own length."""
    n = len(drones)
    planned = make_planner(drones, bridge_s, hold_positions(recorder, rest_z, t0, n))
    show_duration = duration
    duration = duration + bridge_s
    show = [s for s in recorder.samples if t0 <= s[0] <= t0 + duration and len(s[2]) == n]
    actual = result["actual"]
    if not show:
        actual["gazebo"] = {"error": "no Gazebo samples inside the show window"}
        return

    # Sim health: real-time factor and the longest stall.
    wall_span = show[-1][0] - show[0][0]
    sim_span = show[-1][1] - show[0][1]
    longest_gap = 0.0
    for a, b in zip(recorder.samples, recorder.samples[1:]):
        if t0 - 5 <= a[0] <= t0 + duration + 5:
            longest_gap = max(longest_gap, b[0] - a[0], (b[0] - a[0]) - (b[1] - a[1]))
    gz: dict = {
        "samples_in_show": len(show),
        "sample_rate_hz": round(len(show) / wall_span, 1) if wall_span > 0 else None,
        "real_time_factor": round(sim_span / wall_span, 3) if wall_span > 0 else None,
        "longest_stall_s": round(longest_gap, 2),
        "sim_stalled": longest_gap > SIM_STALL_S,
    }

    # Minimum pairwise separation (3D), and where it happened.
    best_d, best_pair, best_t = math.inf, (0, 0), 0.0
    for wall, _sim, poses in show:
        actual_pts = {i: to_fleet(poses[i], rest_z[i]) for i in range(n)}
        for i in range(n):
            for j in range(i + 1, n):
                d = math.dist(actual_pts[i], actual_pts[j])
                if d < best_d:
                    best_d, best_pair, best_t = d, (i, j), wall - t0
    if n >= 2:
        gz["min_separation_m"] = round(best_d, 3)
        gz["min_separation_pair"] = list(best_pair)
        gz["min_separation_at_s"] = round(best_t, 1)

    # Planned separation at the same instants, so the two are comparable.
    planned_min = math.inf
    for wall, _sim, _p in show:
        planned_pts = [planned(i, wall - t0) for i in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                planned_min = min(planned_min, math.dist(planned_pts[i], planned_pts[j]))
    if n >= 2:
        gz["planned_min_separation_same_instants_m"] = round(planned_min, 3)

    # Tracking error: Gazebo position vs the planned point at the same time.
    per_drone = {}
    worst: tuple[float, int, float] = (0.0, 0, 0.0)
    for i in range(n):
        errs = []
        for wall, _sim, poses in show:
            e = math.dist(to_fleet(poses[i], rest_z[i]), planned(i, wall - t0))
            errs.append((wall - t0, e))
            if e > worst[0]:
                worst = (e, i, wall - t0)
        vals = [e for _t, e in errs]
        per_drone[str(i)] = {
            "max_m": round(max(vals), 3),
            "rms_m": round(math.sqrt(sum(v * v for v in vals) / len(vals)), 3),
            "p95_m": round(sorted(vals)[int(0.95 * (len(vals) - 1))], 3),
            **start_transient(errs),
        }
    gz["tracking_error"] = per_drone
    gz["bridge_s"] = bridge_s
    gz["start_max_descent_rate_ms"] = start_descent_rate(show, rest_z, n, t0, bridge_s + 3.0)
    gz["max_tracking_error_m"] = round(worst[0], 3)
    gz["max_tracking_error_drone"] = worst[1]
    gz["max_tracking_error_at_s"] = round(worst[2], 1)

    # Where each drone ended up vs its planned final point (its pad).
    last = recorder.samples[-1][2]
    gz["final_position_error_m"] = {
        str(i): round(math.dist(to_fleet(last[i], rest_z[i]), interpolate(drones[i], show_duration)), 3)
        for i in range(n) if i in last
    }

    # The GCS's own view of the same flight: its fleet-frame telemetry (0.5 s
    # samples) against the plan, and its mean offset from Gazebo's truth. A
    # consistent offset is a frame or pad-placement error, not tracking.
    if gcs_rows:
        gcs_err: dict[str, float] = {}
        offsets: dict[int, list[tuple[float, float, float]]] = {i: [] for i in range(n)}
        walls = [s[0] for s in recorder.samples]
        for row in gcs_rows:
            t = float(row["timestamp"])
            i = int(row["drone_id"])
            if i not in drones or not (t0 <= t <= t0 + duration):
                continue
            g = (float(row["x"]), float(row["y"]), float(row["z"]))
            e = math.dist(g, planned(i, t - t0))
            gcs_err[str(i)] = max(gcs_err.get(str(i), 0.0), e)
            k = bisect.bisect_left(walls, t)
            if 0 < k < len(walls) and i in recorder.samples[k][2]:
                truth = to_fleet(recorder.samples[k][2][i], rest_z[i])
                offsets[i].append((g[0] - truth[0], g[1] - truth[1], g[2] - truth[2]))
        gz["gcs_reported_max_tracking_error_m"] = {k: round(v, 3) for k, v in sorted(gcs_err.items())}
        gz["gcs_minus_gazebo_mean_offset_m"] = {
            str(i): [round(statistics.mean(c[a] for c in offs), 2) for a in range(3)]
            for i, offs in offsets.items() if offs
        }
    actual["gazebo"] = gz

    # Raw Gazebo samples for the record (fleet frame, 10 Hz thinned).
    with open(out_dir / f"{label}.gazebo_fleet_frame.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_show_s", "sim_s", "drone_id", "x_north", "y_east", "alt", "planned_x", "planned_y", "planned_alt"])
        last_kept = -math.inf
        for wall, sim, poses in recorder.samples:
            if wall - last_kept < 0.1:
                continue
            last_kept = wall
            for i, p in sorted(poses.items()):
                x, y, z = to_fleet(p, rest_z[i])
                px, py, pz = planned(i, wall - t0)
                w.writerow([f"{wall - t0:.2f}", f"{sim:.3f}", i, f"{x:.3f}", f"{y:.3f}", f"{z:.3f}",
                            f"{px:.3f}", f"{py:.3f}", f"{pz:.3f}"])


#: Error below which a drone counts as having caught up with the plan after the
#: start. The GCS holds formation at altitude_layers() (2, 4, 6 ... m) while a
#: Studio show starts on the pad (z = 0), so every drone first dives toward its
#: pad; this splits that transient from steady tracking.
TRANSIENT_DONE_M = 0.5


def start_transient(errs: list[tuple[float, float]]) -> dict:
    """``errs`` is [(t_show_s, error_m)] in time order. Returns how long the
    start transient lasted and the worst error after it. A drone that never
    gets within TRANSIENT_DONE_M has ``start_transient_s`` None and its whole
    show counts, so a constant offset is not hidden."""
    done = next((t for t, e in errs if t >= 0 and e < TRANSIENT_DONE_M), None)
    after = [e for t, e in errs if done is None or t >= done]
    return {
        "start_transient_s": round(done, 2) if done is not None else None,
        "max_after_transient_m": round(max(after), 3) if after else None,
    }


def rescore(paths: list[str]) -> None:
    """Recompute the start-transient fields of saved runs from their
    ``.gazebo_fleet_frame.csv`` (10 Hz thinned), in place."""
    for name in paths:
        p = Path(name)
        r = json.loads(p.read_text())
        if "verdict" not in r or "label" not in r:
            continue
        csv_path = p.with_name(f"{r['label']}.gazebo_fleet_frame.csv")
        gz = (r.get("actual") or {}).get("gazebo")
        if not gz or not csv_path.exists():
            continue
        duration = r["planned"]["duration_s"]
        errs: dict[str, list[tuple[float, float]]] = {}
        for row in csv.DictReader(csv_path.open()):
            t = float(row["t_show_s"])
            if 0 <= t <= duration:
                a = [float(row[k]) for k in ("x_north", "y_east", "alt")]
                b = [float(row[k]) for k in ("planned_x", "planned_y", "planned_alt")]
                errs.setdefault(row["drone_id"], []).append((t, math.dist(a, b)))
        for i, e in errs.items():
            entry = gz["tracking_error"].setdefault(i, {})
            entry.pop("max_after_5s_m", None)
            entry.update(start_transient(e))
            entry["rescored_from_csv_10hz"] = True
        r["verdict"] = verdict(r)
        p.write_text(json.dumps(r, indent=2))
        print(f"rescored {p}")


def verdict(result: dict) -> str:
    """PASS needs: certified + signed + streamed start, the show completing with
    no abort or emergency, a live (not stalled) sim, Gazebo separation at or
    above the GCS's own minimum, and every drone disarmed at the end.
    DISCARD is a frozen/diverged sim: rerun it, do not count it."""
    if result.get("error"):
        return "ERROR"
    actual = result.get("actual", {})
    gz = actual.get("gazebo", {})
    if gz.get("sim_stalled"):
        return "DISCARD (sim stalled)"
    limit = (result.get("planned", {}).get("limits") or {}).get("min_separation_m", 1.5)
    reasons = []
    if actual.get("end_state") != "SHOW_COMPLETE":
        reasons.append(f"ended in {actual.get('end_state')}")
    if actual.get("aborted_drones"):
        reasons.append(f"aborted drones {actual['aborted_drones']}")
    if actual.get("emergency_transitions"):
        reasons.append("emergency transition")
    if gz.get("min_separation_m") is None or gz["min_separation_m"] < limit:
        reasons.append(f"Gazebo min separation {gz.get('min_separation_m')} m < {limit} m")
    if actual.get("final_state") != "DISARMED":
        reasons.append(f"final state {actual.get('final_state')}")
    if reasons:
        return "FAIL: " + "; ".join(reasons)
    # The GCS streams on wall-clock time; a sim running slower than real time
    # falls behind the plan even though nothing failed.
    rtf = gz.get("real_time_factor")
    if rtf is not None and rtf < DEGRADED_RTF:
        return f"PASS (degraded: real-time factor {rtf})"
    return "PASS"


#: Below this Gazebo real-time factor a passing run is flagged as degraded.
DEGRADED_RTF = 0.9


# --- Summary --------------------------------------------------------------

def summarize(paths: list[str]) -> str:
    # A shell glob over the output dir also matches the saved show and its
    # .sig.json; keep only run records.
    rows = [r for r in (json.loads(Path(p).read_text()) for p in paths)
            if isinstance(r, dict) and "verdict" in r and "label" in r]
    head = ("| Run | Drones | Verdict | Planned duration (s) | Actual playback (s) | Studio min sep (m) "
            "| Gazebo min sep (m) | Max tracking error (m) | Start transient (s) | Max error after transient (m) "
            "| RTF | Abort |")
    lines = [head, "|" + "---|" * 12]
    for r in rows:
        p, a = r.get("planned", {}), r.get("actual", {})
        g = a.get("gazebo", {})
        te = g.get("tracking_error", {}).values()
        after = [v["max_after_transient_m"] for v in te if v.get("max_after_transient_m") is not None]
        transient = [v["start_transient_s"] for v in te if v.get("start_transient_s") is not None]
        never = [k for k, v in g.get("tracking_error", {}).items() if v.get("start_transient_s") is None]
        transient_txt = f"never (drones {', '.join(never)})" if never else (max(transient) if transient else "")
        aborts = [t["trigger"] for t in a.get("emergency_transitions", [])]
        if a.get("aborted_drones"):
            aborts.insert(0, f"drones {a['aborted_drones']} aborted")
        abort = ", ".join(aborts) or "none"
        lines.append(
            f"| {r['label']} | {r.get('num_drones', '?')} | {r.get('verdict')} | {p.get('duration_s', '')} "
            f"| {a.get('playback_duration_s', '')} | {p.get('min_separation_m', '')} | {g.get('min_separation_m', '')} "
            f"| {g.get('max_tracking_error_m', '')} (drone {g.get('max_tracking_error_drone', '')} at {g.get('max_tracking_error_at_s', '')} s) "
            f"| {transient_txt} | {max(after) if after else ''} "
            f"| {g.get('real_time_factor', '')} | {abort} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="fly one show and record planned vs actual")
    src = r.add_mutually_exclusive_group()
    src.add_argument("--show", help="choreo JSON ({drones: [...]}) exported from Studio")
    src.add_argument("--drones", type=int, default=3, help="build the Studio-sequencer stand-in for N drones")
    r.add_argument("--rate-hz", type=float, default=10.0, help="stand-in build rate (Studio default since #160)")
    r.add_argument("--gcs", default="http://127.0.0.1:8090")
    r.add_argument("--studio", default="http://127.0.0.1:8001")
    r.add_argument("--operator", default=os.environ.get("TARA_OPERATOR", "sitl172"))
    r.add_argument("--passphrase", default=os.environ.get("TARA_PASSPHRASE", ""))
    r.add_argument("--world", default="drone_show_field")
    r.add_argument("--out", required=True)
    r.add_argument("--label")
    r.add_argument("--commit", help="commit SHA to record (default: git rev-parse HEAD)")
    r.add_argument("--connect-timeout", type=float, default=90)
    r.add_argument("--launch-timeout", type=float, default=180)
    r.add_argument("--hold-s", type=float, default=3.0, help="settle time in FORMATION_HOLD before the start")
    r.add_argument("--show-slack-s", type=float, default=60, help="wait this long past the planned duration")
    r.add_argument("--disarm-wait-s", type=float, default=20, help="wait for auto-disarm before commanding LAND")
    r.add_argument("--land-timeout", type=float, default=120)
    s = sub.add_parser("summarize", help="Markdown table from run JSONs")
    s.add_argument("paths", nargs="+")
    rs = sub.add_parser("rescore", help="recompute start-transient fields from saved CSVs")
    rs.add_argument("paths", nargs="+")
    args = ap.parse_args(argv)
    if args.cmd == "summarize":
        print(summarize(args.paths))
        return 0
    if args.cmd == "rescore":
        rescore(args.paths)
        return 0
    result = run(args)
    return 0 if result["verdict"].startswith("PASS") else 1


if __name__ == "__main__":
    sys.exit(main())
