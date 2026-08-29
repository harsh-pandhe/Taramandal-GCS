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
        manager.start_trajectory(_short_show(), mode="LOCAL")
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

        manager.start_trajectory(_short_show(duration=5.0), mode="LOCAL")
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

        manager.start_trajectory(_short_show(duration=5.0), mode="LOCAL")
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

    manager.start_trajectory(_short_show(duration=5.0), mode="LOCAL")
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
    first.start_trajectory(_short_show(duration=5.0), mode="LOCAL")
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
    first.start_trajectory(_short_show(duration=5.0), mode="LOCAL")
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


# --- The acceptance-criteria test: real process, real kill, real restart ---

GCS_STARTUP_TIMEOUT_S = 20.0


def _wait_for_server(base_url: str, timeout: float) -> None:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{base_url}/api/saved-show-state", timeout=1.0)
            return
        except Exception as e:
            last_error = e
            time.sleep(0.3)
    raise RuntimeError(f"GCS server did not come up at {base_url}: {last_error}")


def _post(base_url: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(f"{base_url}{path}", data=data, method="POST",
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        return json.loads(resp.read())


def _get(base_url: str, path: str) -> dict:
    with urllib.request.urlopen(f"{base_url}{path}", timeout=5.0) as resp:
        return json.loads(resp.read())


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
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            upload_result = json.loads(resp.read())
        assert upload_result["status"] == "success"

        _post(base_url, "/api/start-trajectory", {"mode": "LOCAL", "trigger_epoch_ms": 0})

        # Let the show run long enough for at least one periodic snapshot.
        # (STATE_SNAPSHOT_INTERVAL_S is 5s in production code; wait past it.)
        time.sleep(6.5)

        # Hard kill — no shutdown hook, no graceful cleanup, simulating a crash.
        proc.kill()
        proc.wait(timeout=10)
        proc = None

        assert os.path.exists(state_path), "expected a periodic snapshot to have been written before the kill"

        # Restart a fresh process pointed at the same state file.
        proc = _spawn()
        _wait_for_server(base_url, GCS_STARTUP_TIMEOUT_S)

        saved = _get(base_url, "/api/saved-show-state")
        assert saved["pending"] is True
        assert saved["mode"] == "LOCAL"
        assert saved["elapsed_seconds"] > 0
        assert "0" in saved["drones_summary"]

        resumed = _post(base_url, "/api/resume-show")
        assert resumed["status"] == "success"
        assert "0" in resumed["trajectory_data"] or 0 in resumed["trajectory_data"]

        # Resuming must not have re-armed/launched anything on its own.
        state_after_resume = _get(base_url, "/api/saved-show-state")
        assert state_after_resume["pending"] is False
    finally:
        if proc is not None:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
