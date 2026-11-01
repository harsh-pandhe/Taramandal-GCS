"""The mission directory must exist on the drone before an upload, or the
upload fails loudly saying so (issue #172, F2).

#217 made the directory creation recursive, but it still swallowed every
error: PX4's FTP server reports an *existing* directory as an error, so the
failures could not be told from the ones that matter. When a directory could
not be created (read-only storage, no SD card) the operator got MAVLink FTP's
bare FILE_DOES_NOT_EXIST from the upload itself, naming no directory. These
tests pin the check that follows the creation: the target directory is listed,
and if it is not there the drone's mission is FAILED with a message that says
which directory and why.
"""

import asyncio

import pytest

from tara_core import altitude_layers
from tara_gcs.drone_manager import MISSION_REMOTE_DIR, DroneManager, _pad_offset


def _show(n: int) -> dict:
    alts = altitude_layers(n)
    show = {}
    for i in range(n):
        x, y = _pad_offset(i)
        show[i] = [
            {"time": 0.0, "x": x, "y": y, "z": -alts[i], "yaw": 0.0},
            {"time": 2.0, "x": x + 1.0, "y": y, "z": -alts[i], "yaw": 0.0},
        ]
    return show


def _manager(tmp_path, n: int) -> DroneManager:
    return DroneManager(ports=list(range(n)), backend="sim", state_path=str(tmp_path / "show_state.json"))


async def _connected(manager: DroneManager) -> None:
    await manager.connect_all()
    await asyncio.sleep(0.5)  # telemetry + the armable settle delay


class FtpDirError(Exception):
    """Stands in for mavsdk.ftp.FtpError."""


def _storage_without(manager: DroneManager, drone_id: int, monkeypatch, *, creatable: bool):
    """Make drone ``drone_id``'s FTP behave like a fresh PX4 rootfs: no directory
    exists, ``create_directory`` either works (``creatable``) or is refused, and
    ``list_directory`` raises for a directory that was never created."""
    ftp = manager.drones[drone_id].ftp
    made: set[str] = set()

    async def create_directory(path):
        if not creatable:
            raise FtpDirError("FILE_PROTECTED")
        if path in made:
            raise FtpDirError("FILE_EXISTS")  # PX4 reports an existing directory as an error
        made.add(path)

    async def list_directory(path):
        if path not in made:
            raise FtpDirError("FILE_DOES_NOT_EXIST")
        return []

    monkeypatch.setattr(ftp, "create_directory", create_directory)
    monkeypatch.setattr(ftp, "list_directory", list_directory, raising=False)
    return made


@pytest.mark.asyncio
async def test_an_uncreatable_mission_directory_fails_the_upload_naming_it(tmp_path, monkeypatch):
    manager = _manager(tmp_path, 2)
    try:
        await _connected(manager)
        _storage_without(manager, 0, monkeypatch, creatable=False)

        status = await manager.upload_show(_show(2))

        failed = status["drones"]["0"]
        assert failed["status"] == "FAILED"
        assert MISSION_REMOTE_DIR in failed["msg"]
        assert "FILE_PROTECTED" in failed["msg"]  # and the reason, not just the symptom
        assert status["drones"]["1"]["status"] == "VERIFIED"
        assert not status["all_verified"]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_a_fresh_rootfs_gets_every_directory_in_the_path_and_verifies(tmp_path, monkeypatch):
    manager = _manager(tmp_path, 1)
    try:
        await _connected(manager)
        made = _storage_without(manager, 0, monkeypatch, creatable=True)

        status = await manager.upload_show(_show(1))

        assert status["all_verified"]
        assert made == {"/fs", "/fs/microsd", MISSION_REMOTE_DIR}
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_directories_that_already_exist_are_not_a_failure(tmp_path, monkeypatch):
    """A second upload finds every directory there: each create_directory
    raises, the listing succeeds, and the upload goes ahead."""
    manager = _manager(tmp_path, 1)
    try:
        await _connected(manager)
        _storage_without(manager, 0, monkeypatch, creatable=True)

        assert (await manager.upload_show(_show(1)))["all_verified"]
        assert (await manager.upload_show(_show(1)))["all_verified"]
    finally:
        await manager.shutdown()
