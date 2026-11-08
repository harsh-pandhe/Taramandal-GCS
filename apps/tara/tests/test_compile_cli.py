"""`tara compile` CLI command tests (issue #56)."""

import json

from typer.testing import CliRunner

from tara_cli.main import app
from tara_core import MANIFEST_FILENAME, TARM_EXTENSION, decompile_drone_mission

runner = CliRunner()


def _valid_show_dict():
    return {
        "version": 1,
        "drones": [
            {
                "id": 0,
                "waypoints": [
                    {"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0, "r": 255, "g": 0, "b": 0},
                    {"time": 2.0, "x": 5.0, "y": 0.0, "z": -10.0, "yaw": 90.0, "r": 0, "g": 255, "b": 0},
                ],
            },
            {
                "id": 1,
                "waypoints": [
                    {"time": 0.0, "x": 10.0, "y": 0.0, "z": -2.0, "yaw": 0.0, "r": 0, "g": 0, "b": 255},
                    {"time": 2.0, "x": 15.0, "y": 0.0, "z": -10.0, "yaw": 90.0, "r": 255, "g": 255, "b": 0},
                ],
            },
        ],
    }


def test_compile_json_show_creates_tarm_files_and_manifest(tmp_path):
    show_file = tmp_path / "show.json"
    show_file.write_text(json.dumps(_valid_show_dict()))
    dist_dir = tmp_path / "dist"

    result = runner.invoke(app, ["compile", str(show_file), "--output-dir", str(dist_dir)])
    assert result.exit_code == 0, result.output
    assert "[tara] compiled show" in result.output
    assert "wrote manifest.json" in result.output

    drone0_tarm = dist_dir / f"drone_0{TARM_EXTENSION}"
    drone1_tarm = dist_dir / f"drone_1{TARM_EXTENSION}"
    manifest_json = dist_dir / MANIFEST_FILENAME

    assert drone0_tarm.is_file()
    assert drone1_tarm.is_file()
    assert manifest_json.is_file()

    # Decompile and verify payload integrity
    hdr, wps = decompile_drone_mission(drone0_tarm.read_bytes())
    assert hdr.drone_id == 0
    assert hdr.waypoint_count == 2
    assert len(wps) == 2
    assert wps[0]["r"] == 255


def test_compile_csv_show_creates_valid_binaries(tmp_path):
    csv_file = tmp_path / "show.csv"
    csv_content = (
        "drone_id,time,x,y,z,yaw\n"
        "0,0.0,0.0,0.0,-2.0,0.0\n"
        "0,2.0,4.0,2.0,-6.0,45.0\n"
        "1,0.0,8.0,0.0,-2.0,0.0\n"
        "1,2.0,12.0,2.0,-6.0,45.0\n"
    )
    csv_file.write_text(csv_content)
    dist_dir = tmp_path / "dist_csv"

    result = runner.invoke(app, ["compile", str(csv_file), "--output-dir", str(dist_dir)])
    assert result.exit_code == 0, result.output
    assert (dist_dir / f"drone_0{TARM_EXTENSION}").is_file()
    assert (dist_dir / f"drone_1{TARM_EXTENSION}").is_file()


def test_compile_refuses_unsafe_show_by_default(tmp_path):
    # Show that breaches the 120m altitude limit (e.g. z = -150.0m)
    bad_show = {
        "version": 1,
        "drones": [
            {
                "id": 0,
                "waypoints": [
                    {"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
                    {"time": 2.0, "x": 0.0, "y": 0.0, "z": -150.0, "yaw": 0.0},
                ],
            }
        ],
    }
    show_file = tmp_path / "unsafe_show.json"
    show_file.write_text(json.dumps(bad_show))
    dist_dir = tmp_path / "dist_unsafe"

    result = runner.invoke(app, ["compile", str(show_file), "--output-dir", str(dist_dir)])
    assert result.exit_code == 1
    assert "safety error: Altitude violation" in result.output
    assert not (dist_dir / f"drone_0{TARM_EXTENSION}").exists()


def test_compile_allows_bypassing_safety_validation(tmp_path):
    bad_show = {
        "version": 1,
        "drones": [
            {
                "id": 0,
                "waypoints": [
                    {"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
                    {"time": 2.0, "x": 0.0, "y": 0.0, "z": -150.0, "yaw": 0.0},
                ],
            }
        ],
    }
    show_file = tmp_path / "unsafe_show.json"
    show_file.write_text(json.dumps(bad_show))
    dist_dir = tmp_path / "dist_bypassed"

    result = runner.invoke(app, ["compile", str(show_file), "--output-dir", str(dist_dir), "--no-validate"])
    assert result.exit_code == 0, result.output
    assert (dist_dir / f"drone_0{TARM_EXTENSION}").is_file()


def test_compile_refuses_malformed_file(tmp_path):
    bogus_file = tmp_path / "bogus.json"
    bogus_file.write_text("not json content {[[")
    dist_dir = tmp_path / "dist_bogus"

    result = runner.invoke(app, ["compile", str(bogus_file), "--output-dir", str(dist_dir)])
    assert result.exit_code == 1
    assert "failed to load show" in result.output
