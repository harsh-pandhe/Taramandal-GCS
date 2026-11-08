"""`tara compile` must write the bytes the GCS uploads (issue #56 audit, finding 1).

The mission header promises the drone's own local NED frame, so each file is compiled with
its drone's launch-pad offset. `DroneManager.upload_show` does that; the CLI first did not
(it wrote the shared fleet frame), so for any drone not on the origin pad the two differed
and a CLI file loaded onto that drone would have commanded a sideways move at t = 0.

The reference here is the real `DroneManager` on `backend="sim"`: the bytes compared are the
ones that landed in each simulated drone's onboard storage, not a second call to the same
encoder.
"""

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tara_cli.main import app
from tara_core import MANIFEST_FILENAME, TARM_EXTENSION, Show, decompile_drone_mission
from tara_gcs.drone_manager import MISSION_FILE_NAME, MISSION_REMOTE_DIR, DroneManager

runner = CliRunner()

DIWALI_3 = Path(__file__).resolve().parents[3] / "examples" / "diwali" / "segments" / "diwali_3drone_segment.json"
REMOTE_PATH = f"{MISSION_REMOTE_DIR}/{MISSION_FILE_NAME}"

# Pinned from the committed Diwali 3-drone segment compiled in each drone's pad frame. These
# change if the show file, the pad grid (tara_core.launch_grid / #167), the quantisation or
# the wire format change, which is exactly when someone should look at them. Regenerate with
# `tara compile examples/diwali/segments/diwali_3drone_segment.json -o <dir>` and read
# manifest.json, after checking the change was intended.
GOLDEN_SHOW_ID = "9f10820941538ab1b26609bcdd2b98d0"
GOLDEN_MANIFEST_SHA256 = "13c36bee3fca3429dc6aedef4201b5ebe9a4a67b48043a0d73afe020be61e233"
GOLDEN_DRONE_SHA256 = {
    0: "cef8956bff0adab6bdf64d84149c55f3231004ced1ed52c2e5113077023be43b",
    1: "08718b946bee3d720e027b87ac81faa6350a19bab07482c2f9946fa113fbb35c",
    2: "5a6b994318e79705e6b4c3a5e81b613f26e6d58fb2ffa90dea25c6fccbc7d41a",
}


def _compile_cli(show_file: Path, out_dir: Path) -> dict:
    result = runner.invoke(app, ["compile", str(show_file), "--output-dir", str(out_dir)])
    assert result.exit_code == 0, result.output
    return json.loads((out_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))


async def test_cli_bytes_equal_the_gcs_upload_for_every_drone(tmp_path):
    trajectory = Show.from_any(json.loads(DIWALI_3.read_text(encoding="utf-8"))).drones
    assert sorted(trajectory) == [0, 1, 2]
    out_dir = tmp_path / "dist"
    _compile_cli(DIWALI_3, out_dir)

    manager = DroneManager(ports=[0, 1, 2], backend="sim", state_path=str(tmp_path / "show_state.json"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)
        status = await manager.upload_show(trajectory)
        assert status["all_verified"], status

        stored = manager._sim_fleet.state.storage
        for drone_id in (0, 1, 2):
            cli_bytes = (out_dir / f"drone_{drone_id}{TARM_EXTENSION}").read_bytes()
            assert cli_bytes == stored[drone_id][REMOTE_PATH], f"drone {drone_id}: CLI and GCS files differ"
    finally:
        await manager.shutdown()


def test_every_drone_file_starts_at_its_own_pad(tmp_path):
    """Independent of the GCS: in the drone's own frame, a show that begins on the pad begins at 0, 0.

    In the fleet frame drone 1 would start at y = 2.2 and drone 2 at y = 4.4."""
    out_dir = tmp_path / "dist"
    _compile_cli(DIWALI_3, out_dir)
    for drone_id in (0, 1, 2):
        header, wps = decompile_drone_mission((out_dir / f"drone_{drone_id}{TARM_EXTENSION}").read_bytes())
        assert header.drone_id == drone_id
        assert (wps[0]["x"], wps[0]["y"]) == (0.0, 0.0), f"drone {drone_id} does not start at its own pad"


def test_golden_digests_for_the_diwali_3_drone_segment(tmp_path):
    manifest = _compile_cli(DIWALI_3, tmp_path / "dist")
    assert manifest["show_id"] == GOLDEN_SHOW_ID
    assert manifest["manifest_sha256"] == GOLDEN_MANIFEST_SHA256
    assert {int(k): v["sha256"] for k, v in manifest["drones"].items()} == GOLDEN_DRONE_SHA256


def test_a_slot_with_no_launch_pad_is_refused_not_compiled_in_the_wrong_frame(tmp_path):
    show = {"drones": [{"id": 99999, "waypoints": [{"time": 0.0, "x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0}]}]}
    show_file = tmp_path / "show.json"
    show_file.write_text(json.dumps(show))
    result = runner.invoke(app, ["compile", str(show_file), "--output-dir", str(tmp_path / "dist")])
    assert result.exit_code == 1
    assert "outside supported fleet size" in result.output
    assert not list((tmp_path / "dist").glob("*.tarm"))
