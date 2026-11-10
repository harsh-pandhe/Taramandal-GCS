import json
import os

import pytest

from tara_gcs import state_store
from tara_gcs.state_store import discard_show_state, load_show_state, prime_trajectory_cache, save_show_state


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


# --- The trajectory is encoded once per loaded show, not once per snapshot ----------------------------------------
# A snapshot is written every few seconds while a show plays. Re-encoding a 200 MB trajectory each time froze the GCS
# event loop for about 3 s (269 drones x 9,722 waypoints), which stalled the simulated fleet behind its plan.

@pytest.fixture(autouse=True)
def _fresh_cache():
    state_store.release_trajectory_cache()
    yield
    state_store.release_trajectory_cache()


def _show(n=3, x0=0.0):
    return {i: [{"time": 0.0, "x": x0 + i, "y": 0.0, "z": 0.0, "yaw": 0.0}, {"time": 5.0, "x": x0 + i, "y": 0.0, "z": -3.0, "yaw": 0.0}] for i in range(n)}


def test_snapshot_file_is_the_same_document_the_dict_payload_produced(tmp_path):
    path = str(tmp_path / "s.json")
    trajectory, telemetry = _show(), {0: {"connected": True, "fleet_x": 1.5}, 2: {"connected": False}}
    save_show_state(path, trajectory, mode="GLOBAL", show_started_at=1234.5, telemetry=telemetry)
    on_disk = json.load(open(path))
    assert on_disk["version"] == 1 and on_disk["mode"] == "GLOBAL" and on_disk["show_started_at"] == 1234.5
    assert on_disk["trajectory"] == {str(k): v for k, v in trajectory.items()}
    assert on_disk["telemetry"] == json.loads(json.dumps(telemetry))  # int keys become strings, as before
    assert isinstance(on_disk["saved_at"], float)


def test_the_trajectory_is_encoded_once_across_snapshots(tmp_path, monkeypatch):
    calls = {"n": 0}
    real = json.dumps

    def counting(obj, *a, **k):
        if isinstance(obj, dict) and obj and all(isinstance(v, list) and v and isinstance(v[0], dict) and "time" in v[0] for v in obj.values()):
            calls["n"] += 1
        return real(obj, *a, **k)

    monkeypatch.setattr(state_store.json, "dumps", counting)
    trajectory = _show()
    for _ in range(4):
        save_show_state(str(tmp_path / "s.json"), trajectory, mode="LOCAL", show_started_at=1.0, telemetry={})
    assert calls["n"] == 1
    assert load_show_state(str(tmp_path / "s.json"))["trajectory"][2] == trajectory[2]


def test_priming_at_upload_means_the_first_snapshot_does_not_encode(tmp_path):
    trajectory = _show()
    assert state_store._trajectory_cache["json"] is None
    prime_trajectory_cache(trajectory)
    primed = state_store._trajectory_cache["json"]
    assert primed is not None and state_store._trajectory_cache["ref"] is trajectory
    save_show_state(str(tmp_path / "s.json"), trajectory, mode="LOCAL", show_started_at=1.0, telemetry={})
    assert state_store._trajectory_cache["json"] is primed  # the very same encoding was reused, not rebuilt


def test_a_new_show_is_encoded_afresh_and_never_serves_the_old_one(tmp_path):
    path = str(tmp_path / "s.json")
    first, second = _show(x0=0.0), _show(x0=100.0)
    save_show_state(path, first, mode="LOCAL", show_started_at=1.0, telemetry={})
    save_show_state(path, second, mode="LOCAL", show_started_at=2.0, telemetry={})
    assert load_show_state(path)["trajectory"][0][0]["x"] == 100.0


def test_a_slot_that_grows_in_place_is_not_served_stale(tmp_path):
    path = str(tmp_path / "s.json")
    trajectory = _show()
    save_show_state(path, trajectory, mode="LOCAL", show_started_at=1.0, telemetry={})
    trajectory[1].append({"time": 9.0, "x": 1.0, "y": 0.0, "z": -1.0, "yaw": 0.0})
    save_show_state(path, trajectory, mode="LOCAL", show_started_at=1.0, telemetry={})
    assert len(load_show_state(path)["trajectory"][1]) == 3


def test_discarding_the_state_releases_the_cached_encoding(tmp_path):
    path = str(tmp_path / "s.json")
    save_show_state(path, _show(), mode="LOCAL", show_started_at=1.0, telemetry={})
    assert state_store._trajectory_cache["json"] is not None
    discard_show_state(path)
    assert state_store._trajectory_cache["json"] is None
