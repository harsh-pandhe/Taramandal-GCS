"""Issue #4: persist active trajectory + telemetry so GCS can resume after a restart.

`test_kill_and_restart_gcs_process_recovers_state` below is the acceptance
criterion's own test: it launches the real GCS FastAPI app as a separate OS
process against the sim backend, uploads and starts a trajectory, kills the
process with SIGKILL (no graceful shutdown hook runs), starts a fresh process
pointed at the same state file, and asserts the saved show is there waiting
to be resumed. Everything above it is a faster, in-process unit-level check
of the same DroneManager methods that test exercises end-to-end.
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from tara_gcs.auth import OperatorStore, Role
from tara_gcs.drone_manager import DroneManager


def _short_show(duration: float = 3.0) -> dict:
    return {
        0: [
            {"time": 0.0, "x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0},
            {"time": duration, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
        ],
    }


@pytest.mark.asyncio
async def test_starting_trajectory_records_show_started_at(tmp_path):
    manager = DroneManager(ports=[0], backend="sim", state_path=str(tmp_path / "show_state.json"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.2)

        before = time.time()
        show = _short_show()
        await manager.upload_show(show)
        manager.start_trajectory(show, mode="LOCAL")
        assert manager.show_started_at is not None
        assert manager.show_started_at >= before
        assert manager.show_mode == "LOCAL"
    finally:
        manager.stop_trajectory()
        await manager.shutdown()


@pytest.mark.asyncio
async def test_snapshot_is_written_periodically_not_every_tick(tmp_path):
    state_path = str(tmp_path / "show_state.json")
    manager = DroneManager(ports=[0], backend="sim", state_path=state_path)
    manager.STATE_SNAPSHOT_INTERVAL_S = 0.1  # fast tick for the test only
    try:
        await manager.connect_all()
        await asyncio.sleep(0.2)

        show = _short_show(duration=5.0)
        await manager.upload_show(show)
        manager.start_trajectory(show, mode="LOCAL")
        assert not os.path.exists(state_path)  # nothing written yet at tick 0

        await asyncio.sleep(0.35)  # a few snapshot intervals
        assert os.path.exists(state_path)

        with open(state_path) as f:
            saved = json.load(f)
        assert saved["mode"] == "LOCAL"
        assert "0" in saved["trajectory"]
    finally:
        manager.stop_trajectory()
        await manager.shutdown()


@pytest.mark.asyncio
async def test_stop_trajectory_discards_saved_state(tmp_path):
    state_path = str(tmp_path / "show_state.json")
    manager = DroneManager(ports=[0], backend="sim", state_path=state_path)
    manager.STATE_SNAPSHOT_INTERVAL_S = 0.05
    try:
        await manager.connect_all()
        await asyncio.sleep(0.2)

        show = _short_show(duration=5.0)
        await manager.upload_show(show)
        manager.start_trajectory(show, mode="LOCAL")
        await asyncio.sleep(0.15)
        assert os.path.exists(state_path)

        manager.stop_trajectory()
        assert not os.path.exists(state_path)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_shutdown_mid_show_leaves_state_for_resume(tmp_path):
    """A GCS process shutting down mid-show (Ctrl+C/SIGTERM) should leave a
    resumable snapshot behind — this is the "restart", not "the show ended,"
    path, unlike an operator-triggered stop_trajectory()."""
    state_path = str(tmp_path / "show_state.json")
    manager = DroneManager(ports=[0], backend="sim", state_path=state_path)
    await manager.connect_all()
    await asyncio.sleep(0.2)

    show = _short_show(duration=5.0)
    await manager.upload_show(show)
    manager.start_trajectory(show, mode="LOCAL")
    await asyncio.sleep(0.2)

    await manager.shutdown()

    assert os.path.exists(state_path)
    with open(state_path) as f:
        saved = json.load(f)
    assert saved["mode"] == "LOCAL"


@pytest.mark.asyncio
async def test_resume_pending_show_reloads_trajectory_without_starting_playback(tmp_path):
    state_path = str(tmp_path / "show_state.json")

    # First manager plays a show and "crashes" (shutdown leaves state on disk).
    first = DroneManager(ports=[0], backend="sim", state_path=state_path)
    await first.connect_all()
    await asyncio.sleep(0.2)
    show = _short_show(duration=5.0)
    await first.upload_show(show)
    first.start_trajectory(show, mode="LOCAL")
    await asyncio.sleep(0.3)
    await first.shutdown()

    # A fresh manager (simulating a restarted process) discovers the saved state.
    second = DroneManager(ports=[0], backend="sim", state_path=state_path)
    try:
        pending = second.load_pending_resume_state()
        assert pending is not None
        assert second.active_trajectory is None  # not applied yet — operator hasn't decided

        elapsed = second.resume_pending_show()
        assert elapsed >= 0.2
        assert second.active_trajectory is not None
        assert 0 in second.active_trajectory

        # Resuming must not re-arm/launch or start playback on its own.
        assert second.is_running_trajectory is False
        assert second.trajectory_task is None
        assert not any(d.get("armed") for d in second.telemetry.values())

        # The saved file is consumed once resumed.
        assert not os.path.exists(state_path)
    finally:
        await second.shutdown()


@pytest.mark.asyncio
async def test_discard_pending_show_clears_state_without_applying(tmp_path):
    state_path = str(tmp_path / "show_state.json")

    first = DroneManager(ports=[0], backend="sim", state_path=state_path)
    await first.connect_all()
    await asyncio.sleep(0.2)
    show = _short_show(duration=5.0)
    await first.upload_show(show)
    first.start_trajectory(show, mode="LOCAL")
    await asyncio.sleep(0.2)
    await first.shutdown()

    second = DroneManager(ports=[0], backend="sim", state_path=state_path)
    try:
        second.load_pending_resume_state()
        second.discard_pending_show()

        assert second.active_trajectory is None
        assert second.pending_resume_state is None
        assert not os.path.exists(state_path)
    finally:
        await second.shutdown()


def test_resume_without_pending_state_raises(tmp_path):
    manager = DroneManager(ports=[], state_path=str(tmp_path / "show_state.json"))
    with pytest.raises(ValueError):
        manager.resume_pending_show()


@pytest.mark.asyncio
async def test_unexpected_error_mid_show_propagates_and_still_cleans_up(tmp_path):
    """A non-CancelledError escaping the playback loop must surface as itself
    and still run the finally-block cleanup.

    The loop indexes self.telemetry[drone_id] directly, so a drone dropping out
    of telemetry mid-tick raises KeyError. That path runs neither the
    `except CancelledError` nor an `else` branch, so `cancelled` has to already
    be bound before the try — otherwise the finally block raises
    UnboundLocalError, which both masks the real exception and skips the
    state-discard cleanup.
    """
    state_path = str(tmp_path / "show_state.json")
    manager = DroneManager(ports=[0], backend="sim", state_path=state_path)
    manager.STATE_SNAPSHOT_INTERVAL_S = 0.05
    try:
        await manager.connect_all()
        await asyncio.sleep(0.2)

        show = _short_show(duration=5.0)
        await manager.upload_show(show)
        manager.start_trajectory(show, mode="LOCAL")
        await asyncio.sleep(0.15)
        assert os.path.exists(state_path)

        # Drone drops out of telemetry mid-show while playback is still live.
        manager.telemetry.clear()

        with pytest.raises(KeyError):
            await manager.trajectory_task
        manager.trajectory_task = None

        # finally ran: playback flag cleared and the snapshot discarded.
        assert manager.is_running_trajectory is False
        assert not os.path.exists(state_path)
    finally:
        await manager.shutdown()


# --- The acceptance-criteria test: real process, real kill, real restart ---

GCS_STARTUP_TIMEOUT_S = 20.0


def _wait_for_server(base_url: str, timeout: float) -> None:
    # /api/status takes no operator dependency (it's "for `tara status` and
    # any scripted check" per its own docstring) — unlike /api/saved-show-state,
    # which started requiring auth once RBAC (issue #64) landed on every other
    # endpoint. Probing an authenticated endpoint before any operator/token
    # exists always 401s, never 200s, so this must be the one unauthenticated
    # endpoint, not whichever one the test happens to call first.
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{base_url}/api/status", timeout=1.0)
            return
        except Exception as e:
            last_error = e
            time.sleep(0.3)
    raise RuntimeError(f"GCS server did not come up at {base_url}: {last_error}")


def _post(base_url: str, path: str, payload: dict | None = None, token: str | None = None) -> dict:
    data = json.dumps(payload or {}).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{base_url}{path}", data=data, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        return json.loads(resp.read())


def _get(base_url: str, path: str, token: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(f"{base_url}{path}", headers=headers)
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        return json.loads(resp.read())


def _http_login(base_url: str, operator: str, passphrase: str) -> str:
    req = urllib.request.Request(
        f"{base_url}/api/auth/login", method="POST",
        data=json.dumps({"operator": operator, "passphrase": passphrase}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        return json.loads(resp.read())["token"]


@pytest.mark.slow
def test_kill_and_restart_gcs_process_recovers_state(tmp_path):
    """Kills and restarts the real GCS process mid-show (sim backend) and
    asserts the saved trajectory/playback state is recovered — the exact
    scenario issue #4 asks to be covered by a test."""
    state_path = str(tmp_path / "show_state.json")
    # Bind to port 0 to get an OS-assigned free port, then release it — avoids
    # colliding with a stale/leftover listener on a hardcoded port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

    env = dict(os.environ)
    env["VEHICLE_BACKEND"] = "sim"
    env["NUM_DRONES"] = "1"
    env["GCS_PORT"] = str(port)
    env["GCS_STATE_PATH"] = state_path
    # This test is about crash recovery, not show signing (issue #49), so it
    # uploads a plain unsigned file the way `tara up --allow-unsigned` permits.
    env["GCS_ALLOW_UNSIGNED"] = "1"
    env["GCS_AUDIT_PATH"] = str(tmp_path / "fleet_audit.jsonl")
    # Issue #64: every command endpoint now requires an authenticated
    # operator; a throwaway operators file for this test, not the
    # developer's real one.
    operators_path = tmp_path / "operators.json"
    OperatorStore(operators_path).add("commander", Role.FLIGHT_COMMANDER, "commander passphrase")
    env["GCS_OPERATORS_PATH"] = str(operators_path)
    env["PYTHONPATH"] = os.pathsep.join([
        os.path.join(repo_root, "packages", "tara-core"),
        os.path.join(repo_root, "packages", "tara-gcs"),
        os.path.join(repo_root, "packages", "tara-sim"),
    ])

    def _spawn():
        # Same invocation `tara up` uses in production (apps/tara/tara_cli/services.py) —
        # no --reload, so this is a single OS process to kill, not a
        # reloader-plus-worker pair.
        return subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "tara_gcs.main:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=os.path.join(repo_root, "packages", "tara-gcs"),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    proc = _spawn()
    try:
        _wait_for_server(base_url, GCS_STARTUP_TIMEOUT_S)
        token = _http_login(base_url, "commander", "commander passphrase")

        trajectory = {"0": [
            {"time": 0.0, "x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0},
            {"time": 8.0, "x": 0.0, "y": 0.0, "z": -3.0, "yaw": 0.0},
        ]}

        # There's no raw "set active trajectory" endpoint, so drive it the way
        # the frontend does: upload, then start. We fabricate a minimal JSON
        # trajectory file for the upload endpoint instead of poking internals.
        traj_file = tmp_path / "trajectory.json"
        traj_file.write_text(json.dumps({str(k): v for k, v in trajectory.items()}))

        boundary = "----traja"
        with open(traj_file, "rb") as f:
            file_bytes = f.read()
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="trajectory.json"\r\n'
            f"Content-Type: application/json\r\n\r\n"
        ).encode() + file_bytes + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"{base_url}/api/upload-trajectory",
            data=body,
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Authorization": f"Bearer {token}",
            },
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            upload_result = json.loads(resp.read())
        assert upload_result["status"] == "success"

        # Onboard execution needs each drone to hold a verified copy (#47).
        # The fleet connects in the background after startup, so retry until
        # the drone is up and its mission verifies.
        deadline = time.time() + GCS_STARTUP_TIMEOUT_S
        while True:
            mission = _post(base_url, "/api/upload-mission", token=token)
            if mission.get("all_verified") or time.time() > deadline:
                break
            time.sleep(0.5)
        assert mission["all_verified"], mission

        deadline = time.time() + GCS_STARTUP_TIMEOUT_S
        while True:
            chk = _get(base_url, "/api/preflight-check", token=token)
            if chk.get("launch_permitted") or time.time() > deadline:
                break
            time.sleep(0.2)
        assert chk.get("launch_permitted"), chk

        # The fleet lifecycle (issue #52) refuses to start a show on the
        # ground, so take off first, as an operator would.
        _post(base_url, "/api/launch", token=token)
        deadline = time.time() + GCS_STARTUP_TIMEOUT_S
        while _get(base_url, "/api/fleet-state", token=token)["state"] != "FORMATION_HOLD":
            assert time.time() < deadline, "fleet never reached FORMATION_HOLD after launch"
            time.sleep(0.2)

        _post(base_url, "/api/start-trajectory", {"mode": "LOCAL", "trigger_epoch_ms": 0}, token=token)

        # Let the show run long enough for at least one periodic snapshot.
        # (STATE_SNAPSHOT_INTERVAL_S is 5s in production code; wait past it.)
        time.sleep(6.5)

        # Hard kill — no shutdown hook, no graceful cleanup, simulating a crash.
        proc.kill()
        proc.wait(timeout=10)
        proc = None

        assert os.path.exists(state_path), "expected a periodic snapshot to have been written before the kill"

        # Restart a fresh process pointed at the same state file. Sessions
        # are in-memory, so the old token died with the old process — log in
        # again against the (file-backed, still-present) operators file.
        proc = _spawn()
        _wait_for_server(base_url, GCS_STARTUP_TIMEOUT_S)
        token = _http_login(base_url, "commander", "commander passphrase")

        saved = _get(base_url, "/api/saved-show-state", token=token)
        assert saved["pending"] is True
        assert saved["mode"] == "LOCAL"
        assert saved["elapsed_seconds"] > 0
        assert "0" in saved["drones_summary"]

        resumed = _post(base_url, "/api/resume-show", token=token)
        assert resumed["status"] == "success"
        assert "0" in resumed["trajectory_data"] or 0 in resumed["trajectory_data"]

        # Resuming must not have re-armed/launched anything on its own.
        state_after_resume = _get(base_url, "/api/saved-show-state", token=token)
        assert state_after_resume["pending"] is False
    finally:
        if proc is not None:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
