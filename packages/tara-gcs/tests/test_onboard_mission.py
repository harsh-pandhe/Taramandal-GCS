"""Issue #47: onboard mission pre-upload and a broadcast start trigger, in
place of 10Hz unicast offboard setpoints.

Each acceptance criterion has a test here, run against the real `DroneManager`
on `backend="sim"` rather than a mock:

  * missions uploaded to 100% of connected drones before arming is permitted
    -> test_arming_is_refused_until_the_show_is_uploaded,
       test_one_unverified_copy_blocks_arming_for_the_whole_fleet
  * each drone's reported hash matches the GCS show hash
    -> test_upload_verifies_every_drone_against_the_gcs_show_hash
  * zero unicast position setpoints during the show
    -> test_onboard_show_sends_zero_unicast_setpoints
  * losing the GCS link mid-show neither aborts nor trips offboard failsafe
    -> test_link_loss_mid_show_does_not_abort_the_show, with
       test_link_loss_mid_show_does_abort_a_streamed_show as its control: the
       same scenario on the legacy path *does* fail safe, which is what shows
       the first test is able to catch the failure at all.

The simulator models the drone side: `SimSystem.ftp` writes into per-drone
onboard storage, `SimFleet.broadcast` is the shared radio, `FleetState` flies
stored missions off its own UTC clock and — like PX4's COM_OF_LOSS_T — fails
safe when an offboard drone stops hearing setpoints.
"""

import asyncio
import zlib

import pytest
from fastapi import HTTPException

from tara_core import altitude_layers, compute_show_id, decode_mission
from tara_gcs import main as gcs_main
from tara_gcs.auth import Operator, Role
from tara_gcs.drone_manager import MISSION_FILE_NAME, MISSION_REMOTE_DIR, DroneManager, LaunchGateError, _pad_offset


# Issue #64: route handlers called directly take the operator that the auth
# dependency would otherwise inject.
COMMANDER = Operator("test-commander", Role.FLIGHT_COMMANDER)

REMOTE_PATH = f"{MISSION_REMOTE_DIR}/{MISSION_FILE_NAME}"


def _show(n: int, duration: float = 3.0, north_m: float = 1.0) -> dict:
    """Every drone starts at its own pad and launch altitude, and moves
    together north then up a little — separation is preserved throughout."""
    alts = altitude_layers(n)
    show = {}
    for i in range(n):
        x, y = _pad_offset(i)
        alt = alts[i]
        show[i] = [
            {"time": 0.0, "x": x, "y": y, "z": -alt, "yaw": 0.0},
            {"time": duration / 2, "x": x + north_m, "y": y, "z": -alt, "yaw": 0.0},
            {"time": duration, "x": x + north_m, "y": y, "z": -alt - 0.5, "yaw": 0.0},
        ]
    return show


def _manager(tmp_path, n: int) -> DroneManager:
    return DroneManager(ports=list(range(n)), backend="sim", state_path=str(tmp_path / "show_state.json"))


async def _connected(manager: DroneManager) -> None:
    await manager.connect_all()
    await asyncio.sleep(0.5)  # telemetry + the armable settle delay


async def _launched(manager: DroneManager, show: dict) -> None:
    await _connected(manager)
    status = await manager.upload_show(show)
    assert status["all_verified"], status
    await manager.launch_sequence()
    await asyncio.sleep(1.5)  # climb to launch altitude


async def _wait_for(predicate, timeout: float) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("condition not reached before timeout")
        await asyncio.sleep(0.05)


def _assert_at_final_waypoints(manager: DroneManager, show: dict) -> None:
    fs = manager._sim_fleet.state
    for drone_id, wps in show.items():
        x, y = _pad_offset(drone_id)
        last = wps[-1]
        expected = [last["x"] - x, last["y"] - y, -last["z"]]
        assert fs.position[drone_id] == pytest.approx(expected, abs=0.3), f"drone {drone_id}"


# --- the arming gate ----------------------------------------------------------


@pytest.mark.asyncio
async def test_arming_is_refused_until_the_show_is_uploaded(tmp_path):
    manager = _manager(tmp_path, 3)
    try:
        await _connected(manager)
        with pytest.raises(LaunchGateError, match="upload"):
            await manager.launch_sequence()
        await asyncio.sleep(0.3)
        assert not manager._sim_fleet.state.armed.any()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_upload_verifies_every_drone_against_the_gcs_show_hash(tmp_path):
    n = 4
    show = _show(n)
    manager = _manager(tmp_path, n)
    try:
        await _connected(manager)
        status = await manager.upload_show(show)

        assert status["all_verified"]
        assert status["verified_count"] == n
        assert status["show_id"] == compute_show_id(show).hex()
        assert status["launch_gate"] == {"ok": True, "reason": "", "armable": list(range(n)), "problems": []}

        fs = manager._sim_fleet.state
        for drone_id in range(n):
            stored = fs.storage[drone_id][REMOTE_PATH]
            header, _ = decode_mission(stored)
            # What the drone holds is the GCS's show, addressed to this drone...
            assert header.show_id == compute_show_id(show)
            assert header.drone_id == drone_id
            # ...and its CRC is exactly the one the GCS reported for it.
            assert f"{zlib.crc32(stored):08x}" == status["drones"][str(drone_id)]["crc32"]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_one_unverified_copy_blocks_arming_for_the_whole_fleet(tmp_path):
    n = 3
    manager = _manager(tmp_path, n)
    try:
        await _connected(manager)

        async def _mismatch(local, remote):
            return False

        manager.drones[1].ftp.are_files_identical = _mismatch
        status = await manager.upload_show(_show(n))

        assert not status["all_verified"]
        assert status["drones"]["1"]["status"] == "MISMATCH"
        assert not status["launch_gate"]["ok"]
        with pytest.raises(LaunchGateError, match="drone 1"):
            await manager.launch_sequence()
        await asyncio.sleep(0.3)
        assert not manager._sim_fleet.state.armed.any()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_failed_transfer_blocks_arming(tmp_path):
    n = 2
    manager = _manager(tmp_path, n)
    try:
        await _connected(manager)

        async def _broken_upload(local, remote_dir):
            raise RuntimeError("FTP timeout")
            yield  # pragma: no cover — makes this an async generator, like MAVSDK's

        manager.drones[0].ftp.upload = _broken_upload
        status = await manager.upload_show(_show(n))
        assert status["drones"]["0"]["status"] == "FAILED"
        assert "FTP timeout" in status["drones"]["0"]["msg"]
        assert not manager.launch_gate()["ok"]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_loading_a_different_trajectory_blocks_arming(tmp_path):
    n = 2
    manager = _manager(tmp_path, n)
    try:
        await _connected(manager)
        manager.active_trajectory = _show(n)
        await manager.upload_show(manager.active_trajectory)
        assert manager.launch_gate()["ok"]

        manager.active_trajectory = _show(n, north_m=2.0)
        gate = manager.launch_gate()
        assert not gate["ok"]
        assert "upload it again" in gate["reason"]
    finally:
        await manager.shutdown()


# --- starting and flying the show --------------------------------------------


@pytest.mark.asyncio
async def test_onboard_start_requires_the_uploaded_show(tmp_path):
    n = 2
    manager = _manager(tmp_path, n)
    try:
        await _connected(manager)
        with pytest.raises(ValueError, match="not been uploaded"):
            manager.start_trajectory(_show(n))

        await manager.upload_show(_show(n))
        with pytest.raises(ValueError, match="not been uploaded"):
            manager.start_trajectory(_show(n, north_m=2.0))
        assert manager.trajectory_task is None
    finally:
        await manager.shutdown()


def test_px4_backend_refuses_onboard_start_and_points_to_streamed(tmp_path):
    # Stock PX4 has no onboard show executor and there is no radio wired in.
    manager = DroneManager(ports=[14540], backend="px4", state_path=str(tmp_path / "s.json"))
    with pytest.raises(ValueError, match="streamed"):
        manager.start_trajectory(_show(1))


def test_unknown_execution_is_rejected(tmp_path):
    manager = DroneManager(ports=[14540], backend="px4", state_path=str(tmp_path / "s.json"))
    with pytest.raises(ValueError, match="execution"):
        manager.start_trajectory(_show(1), execution="teleport")


@pytest.mark.asyncio
async def test_onboard_show_sends_zero_unicast_setpoints(tmp_path):
    n = 3
    show = _show(n)
    manager = _manager(tmp_path, n)
    try:
        await _launched(manager, show)
        fs = manager._sim_fleet.state
        setpoints_before_show = fs.setpoints_received.copy()

        manager.start_trajectory(show)
        await _wait_for(lambda: fs.mission_complete.all(), timeout=10.0)

        assert (fs.setpoints_received == setpoints_before_show).all(), (
            f"unicast setpoints sent during the show: {fs.setpoints_received - setpoints_before_show}"
        )
        # Offboard was released, so not even MAVSDK's setpoint re-send ran.
        assert not fs.offboard_active.any()
        # The start went out as broadcast frames, not per-drone commands.
        assert manager.trigger_frames_sent > 0
        _assert_at_final_waypoints(manager, show)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_link_loss_mid_show_does_not_abort_the_show(tmp_path):
    n = 3
    show = _show(n, duration=4.0)
    manager = _manager(tmp_path, n)
    try:
        await _launched(manager, show)
        fs = manager._sim_fleet.state

        manager.start_trajectory(show)
        await _wait_for(lambda: all(mode == "SHOW" for mode in fs.flight_mode), timeout=5.0)
        await asyncio.sleep(0.5)
        fs.connected[:] = False  # the GCS link goes away, for every drone

        await _wait_for(lambda: fs.mission_complete.all(), timeout=10.0)
        assert not fs.offboard_failsafe.any()
        assert fs.armed.all()
        assert all(mode == "HOLD" for mode in fs.flight_mode)  # holding the last waypoint
        _assert_at_final_waypoints(manager, show)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_link_loss_mid_show_does_abort_a_streamed_show(tmp_path):
    """Control for the test above: the legacy path under the same link loss."""
    n = 3
    show = _show(n, duration=4.0)
    manager = _manager(tmp_path, n)
    try:
        await _launched(manager, show)
        fs = manager._sim_fleet.state

        manager.start_trajectory(show, execution="streamed")
        await asyncio.sleep(1.0)
        fs.connected[:] = False

        await _wait_for(lambda: fs.offboard_failsafe.all(), timeout=5.0)
        assert all(mode == "LAND" for mode in fs.flight_mode)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_stop_mid_show_broadcasts_cancel_and_the_fleet_holds(tmp_path):
    n = 3
    show = _show(n, duration=6.0)
    manager = _manager(tmp_path, n)
    try:
        await _launched(manager, show)
        fs = manager._sim_fleet.state

        manager.start_trajectory(show)
        await _wait_for(lambda: all(mode == "SHOW" for mode in fs.flight_mode), timeout=5.0)
        frames = manager.trigger_frames_sent
        manager.stop_trajectory()

        assert manager.trigger_frames_sent == frames + 1  # one CANCEL, to everyone
        assert not fs.mission_active.any()
        assert not fs.mission_complete.any()
        assert fs.armed.all()  # holding, not landing
    finally:
        await manager.shutdown()


# --- API ------------------------------------------------------------------------


@pytest.fixture
def patched_manager(monkeypatch):
    def _install(manager: DroneManager) -> DroneManager:
        monkeypatch.setattr(gcs_main, "drone_manager", manager)
        return manager

    return _install


@pytest.mark.asyncio
async def test_api_launch_is_409_until_the_mission_is_uploaded(tmp_path, patched_manager):
    n = 2
    manager = patched_manager(_manager(tmp_path, n))
    try:
        await _connected(manager)
        manager.active_trajectory = _show(n)

        with pytest.raises(HTTPException) as refused:
            await gcs_main.launch_fleet(operator=COMMANDER)
        assert refused.value.status_code == 409

        assert (await gcs_main.mission_status(operator=COMMANDER))["staged"] is False
        uploaded = await gcs_main.upload_mission(operator=COMMANDER)
        assert uploaded["all_verified"]
        assert (await gcs_main.mission_status(operator=COMMANDER))["launch_gate"]["ok"]

        assert (await gcs_main.launch_fleet(operator=COMMANDER))["status"] == "success"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_api_upload_mission_needs_a_trajectory(tmp_path, patched_manager):
    patched_manager(_manager(tmp_path, 1))
    with pytest.raises(HTTPException) as refused:
        await gcs_main.upload_mission(operator=COMMANDER)
    assert refused.value.status_code == 400


@pytest.mark.asyncio
async def test_upload_mission_creates_directory_recursively(tmp_path, monkeypatch):
    """Issue #172 (F2): MAVLink FTP create_directory is not recursive. On fresh
    SITL rootfs or empty SD cards, /fs/microsd does not exist yet. Verify that
    all path components are created in order."""
    manager = _manager(tmp_path, 1)
    await _connected(manager)
    try:
        created_dirs = []
        original_create_dir = manager.drones[0].ftp.create_directory

        async def spy_create_dir(path: str):
            created_dirs.append(path)
            await original_create_dir(path)

        monkeypatch.setattr(manager.drones[0].ftp, "create_directory", spy_create_dir)

        status = await manager.upload_show(_show(1))
        assert status["all_verified"]
        # Expected sequence for /fs/microsd/tara: /fs, /fs/microsd, /fs/microsd/tara
        assert "/fs" in created_dirs
        assert "/fs/microsd" in created_dirs
        assert "/fs/microsd/tara" in created_dirs
        idx_fs = created_dirs.index("/fs")
        idx_micro = created_dirs.index("/fs/microsd")
        idx_tara = created_dirs.index("/fs/microsd/tara")
        assert idx_fs < idx_micro < idx_tara
    finally:
        await manager.shutdown()

