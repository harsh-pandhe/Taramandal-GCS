"""Issue #126: a validation report states how its numbers were measured.

``min_distance_m`` is the minimum over a sampled sweep, motion metrics are finite
differences of consecutive samples, jerk comes from its own sweep and stencil,
and altitude is read exactly off the waypoints. The report says so (and at
which interval) instead of leaving the reader to guess.
"""

from tara_core.geometry import sweep_times
from tara_studio import server
from tara_studio.validator import DYNAMICS_SWEEP_DT_S, certify_feasibility_report

SHOW = {"drones": [
    {"id": i, "waypoints": [
        {"time": t, "x": 0.0, "y": i * 5.0, "z": -5.0, "yaw": 0.0} for t in (0.0, 2.0)
    ]}
    for i in (0, 1)
]}


def test_certification_report_adds_the_jerk_method_to_the_safety_method():
    _, report = certify_feasibility_report(SHOW)
    method = report["method"]
    assert method["sweep_dt_s"] == 0.1
    assert method["samples"] == len(sweep_times(2.0, 0.1))
    assert method["min_distance"] == "sampled"
    assert method["motion"] == "backward_difference"
    assert method["altitude"] == "waypoint_exact"
    assert method["jerk"] == "central_difference_5pt"
    assert method["jerk_sweep_dt_s"] == DYNAMICS_SWEEP_DT_S == 0.1


def test_api_validate_carries_the_method():
    report = server.api_validate(SHOW)
    assert report["method"]["jerk"] == "central_difference_5pt"
    assert report["method"]["sweep_dt_s"] == report["method"]["jerk_sweep_dt_s"] == 0.1


def test_the_method_does_not_change_status_or_metrics():
    """Purely additive: the same show reports the same verdict and numbers."""
    _, report = certify_feasibility_report(SHOW)
    assert report["status"] == "PASSED"
    assert report["metrics"]["min_distance_m"] == 5.0
    assert report["feasibility_certified"] is True
