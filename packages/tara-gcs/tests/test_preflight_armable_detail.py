"""A PX4 that won't arm because of its flight mode must not read as an EKF fault (issue #171).

Seen on real PX4 v1.16.2 SITL: PX4 left in LAND or POSCTL on the ground reports
is_armable=False, so the ekf_health gate read "Not armable" under the title "EKF
health & home lock". Arming worked again as soon as the mode went back to HOLD;
the EKF was fine all along. The gate is right to stay red; only its words were
wrong. is_armable is one flag covering both causes, so the wording can only say
the mode is a likely cause, never that the EKF is fine.
"""

import pytest

from tara_gcs.preflight import GateState, OperationalProfile, PreArmChecklistEngine


def _ekf_gate(**tel_overrides) -> dict:
    tel = {
        "connected": True,
        "last_heartbeat": None,
        "link_stable_since": 0.0,
        "flight_mode": "HOLD",
        "health_armable": False,
        "health_home_ok": True,
    }
    tel.update(tel_overrides)
    engine = PreArmChecklistEngine(OperationalProfile.SIMULATION, clock=lambda: 1000.0)
    report = engine.evaluate({0: tel}, expected_drones=1, trajectory=None, geofence_radius_m=100.0)
    return next(g for g in report["drones"]["0"]["gates"] if g["rule"] == "ekf_health")


@pytest.mark.parametrize("mode", ["LAND", "POSCTL", "RETURN_TO_LAUNCH"])
def test_not_armable_in_a_non_hold_mode_names_the_mode(mode):
    gate = _ekf_gate(flight_mode=mode)
    assert gate["state"] == GateState.FAIL.value
    assert mode in gate["detail"]
    assert "HOLD" in gate["detail"]


@pytest.mark.parametrize("mode", ["HOLD", "UNKNOWN", None])
def test_not_armable_without_a_mode_to_blame_keeps_the_plain_message(mode):
    gate = _ekf_gate(flight_mode=mode)
    assert gate["state"] == GateState.FAIL.value
    assert gate["detail"] == "Not armable"


def test_missing_home_lock_is_still_reported_alongside_the_mode():
    gate = _ekf_gate(flight_mode="LAND", health_home_ok=False)
    assert gate["state"] == GateState.FAIL.value
    assert "LAND" in gate["detail"]
    assert "no home lock" in gate["detail"].lower()


def test_home_lock_alone_does_not_blame_the_mode():
    """Armable but no home lock: the mode has nothing to do with it."""
    gate = _ekf_gate(flight_mode="LAND", health_armable=True, health_home_ok=False)
    assert gate["state"] == GateState.FAIL.value
    assert gate["detail"] == "No home lock"


def test_armable_passes_in_any_mode():
    """The wording change must not turn a green gate red, or the reverse."""
    gate = _ekf_gate(flight_mode="LAND", health_armable=True)
    assert gate["state"] == GateState.PASS.value
    assert gate["detail"] == "Armable, home position locked"


def test_disconnected_stays_red_whatever_the_flags_say():
    gate = _ekf_gate(connected=False, flight_mode="LAND", health_armable=True)
    assert gate["state"] == GateState.FAIL.value
