"""The pre-arm engine must not re-hash the whole show on every poll.

The dashboard polls /api/preflight-check every few seconds. Re-encoding every
slot (and the whole show) to JSON per poll blocked the GCS event loop for
seconds at 269 drones, which stalled the simulated fleet behind its plan. These
tests pin the memoisation: same answers as before, computed once, and never
stale after the show changes.
"""

from __future__ import annotations

import pytest

from tara_gcs import preflight
from tara_gcs.preflight import PreArmChecklistEngine, show_hash, trajectory_hash

from tara_gcs.drone_manager import _pad_offset

N = 3
NOW = 1000.0


class Clock:
    def __init__(self, t: float = NOW):
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_show(n: int = N) -> dict:
    show = {}
    for i in range(n):
        x, y = _pad_offset(i)
        show[i] = [
            {"time": 0.0, "x": x, "y": y, "z": 0.0, "yaw": 0.0},
            {"time": 5.0, "x": x, "y": y, "z": -3.0 - i, "yaw": 0.0},
        ]
    return show


def fleet(show: dict, n: int = N) -> dict:
    """{drone_id: telemetry} for drones that pass every PRODUCTION gate."""
    out = {}
    for i in range(n):
        x, y = _pad_offset(i)
        out[i] = {
            "id": i, "connected": True, "armed": False, "comm_latency_ms": 20.0, "link_stable_since": NOW - 30.0,
            "gps_fix_type": "RTK_FIXED", "battery_percent": 95.0, "battery_voltage": 16.76,
            "battery_cell_voltages": [4.19, 4.19, 4.18, 4.19], "health_armable": True, "health_home_ok": True,
            "mission_hash": trajectory_hash(show[i]), "local_x": 0.0, "local_y": 0.0, "local_z": 0.0,
            "fleet_x": x, "fleet_y": y, "fleet_z": 0.0,
        }
    return out


@pytest.fixture()
def counted(monkeypatch):
    """Counts real calls to trajectory_hash (the expensive JSON encode + SHA-256)."""
    calls = {"n": 0}
    real = preflight.trajectory_hash

    def counting(waypoints):
        calls["n"] += 1
        return real(waypoints)

    monkeypatch.setattr(preflight, "trajectory_hash", counting)
    return calls


def evaluate(eng, show, n=N):
    return eng.evaluate(fleet(show, n), expected_drones=n, trajectory=show, geofence_radius_m=100.0)


def new_engine():
    return PreArmChecklistEngine("PRODUCTION", clock=Clock())


def test_results_are_identical_to_the_uncached_hashes():
    show = make_show()
    report = evaluate(new_engine(), show)
    assert report["show_hash"] == show_hash(show)
    # The healthy fleet reports each slot's own hash, so every slot's gate must still pass.
    for i in range(N):
        gate = next(g for g in report["drones"][str(i)]["gates"] if g["rule"] == "trajectory_hash")
        assert gate["state"] == "pass", gate


def test_repeated_polls_hash_each_slot_and_the_show_only_once(counted):
    show = make_show()
    eng = new_engine()
    evaluate(eng, show)
    first = counted["n"]
    assert first == N + 1  # one per slot, one for the show
    for _ in range(5):
        evaluate(eng, show)
    assert counted["n"] == first


def test_a_replaced_slot_is_hashed_afresh_and_the_mismatch_is_caught(counted):
    show = make_show()
    eng = new_engine()
    evaluate(eng, show)
    before = counted["n"]
    # The vehicle still reports the hash of the old slot 1, while the GCS now holds a different show for it.
    changed = dict(show)
    changed[1] = [dict(wp, x=wp["x"] + 1.0) for wp in show[1]]
    telemetry = fleet(show)  # onboard hashes computed from the OLD show
    report = eng.evaluate(telemetry, expected_drones=N, trajectory=changed, geofence_radius_m=100.0)
    assert counted["n"] > before
    gate1 = next(g for g in report["drones"]["1"]["gates"] if g["rule"] == "trajectory_hash")
    assert gate1["state"] == "fail"
    assert report["show_hash"] == show_hash(changed) != show_hash(show)


def test_growing_a_slot_in_place_invalidates_its_entry():
    show = make_show()
    eng = new_engine()
    evaluate(eng, show)
    show[2].append({"time": 9.0, "x": 0.0, "y": 0.0, "z": -1.0, "yaw": 0.0})
    report = eng.evaluate(fleet(make_show()), expected_drones=N, trajectory=show, geofence_radius_m=100.0)
    gate2 = next(g for g in report["drones"]["2"]["gates"] if g["rule"] == "trajectory_hash")
    assert gate2["state"] == "fail"
    assert report["show_hash"] == show_hash(show)


def test_unloading_the_show_clears_the_cache():
    show = make_show()
    eng = new_engine()
    evaluate(eng, show)
    report = eng.evaluate(fleet(show), expected_drones=N, trajectory=None, geofence_radius_m=100.0)
    assert report["show_hash"] is None
    assert eng._hashes._slots == {} and eng._hashes._show is None
    # and a fresh load hashes correctly again
    assert evaluate(eng, show)["show_hash"] == show_hash(show)


def test_cached_slot_hash_matches_trajectory_hash():
    show = make_show()
    eng = new_engine()
    for i in range(N):
        assert eng._hashes.slot(i, show[i]) == trajectory_hash(show[i])
