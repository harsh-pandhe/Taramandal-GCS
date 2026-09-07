import os

import pytest

from tara_gcs.state_store import discard_show_state, load_show_state, save_show_state


def test_save_and_load_round_trip(tmp_path):
    path = str(tmp_path / "show_state.json")
    trajectory = {
        0: [{"time": 0.0, "x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0}],
        1: [{"time": 0.0, "x": 1.0, "y": 0.0, "z": 0.0, "yaw": 0.0}],
    }
    telemetry = {0: {"connected": True, "fleet_x": 0.0}}

    save_show_state(path, trajectory, mode="LOCAL", show_started_at=1000.0, telemetry=telemetry)
    loaded = load_show_state(path)

    assert loaded is not None
    assert loaded["mode"] == "LOCAL"
    assert loaded["show_started_at"] == 1000.0
    # Drone-id keys round-trip back to int (JSON forces them to strings on disk).
    assert set(loaded["trajectory"].keys()) == {0, 1}
    assert loaded["trajectory"][0] == trajectory[0]
    assert loaded["telemetry"] == telemetry


def test_load_returns_none_when_no_file(tmp_path):
    path = str(tmp_path / "does_not_exist.json")
    assert load_show_state(path) is None


def test_load_returns_none_on_corrupt_file(tmp_path):
    path = tmp_path / "show_state.json"
    path.write_text("{not valid json")
    assert load_show_state(str(path)) is None


def test_discard_removes_file(tmp_path):
    path = str(tmp_path / "show_state.json")
    save_show_state(path, {0: []}, mode="LOCAL", show_started_at=0.0, telemetry={})
    assert os.path.exists(path)

    discard_show_state(path)
    assert not os.path.exists(path)


def test_discard_is_safe_when_no_file(tmp_path):
    path = str(tmp_path / "does_not_exist.json")
    discard_show_state(path)  # must not raise


def test_save_does_not_leave_tmp_file_behind(tmp_path):
    path = str(tmp_path / "show_state.json")
    save_show_state(path, {0: []}, mode="GLOBAL", show_started_at=5.0, telemetry={})

    leftovers = [f for f in os.listdir(tmp_path) if f != "show_state.json"]
    assert leftovers == []
