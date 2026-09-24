"""Issue #60: jerk detection, DynamicLimits.max_jerk_ms3, and certify_feasibility."""

from __future__ import annotations

from tara_core.geometry import quintic_segment
from tara_studio.validator import DynamicLimits, certify_feasibility, validate_dynamics


def _sharp_reversal_choreo():
    """A drone that flies forward then instantly reverses direction.

    The velocity sign-flip at t=1 is exactly the kind of sharp transition
    issue #60 targets: interpolate_waypoint's linear segments make velocity
    piecewise-constant, so this corner is a large, easily-detected jerk spike
    once sampled at validate_dynamics's default dt.
    """
    return {
        "drones": [
            {
                "id": 0,
                "waypoints": [
                    {"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
                    {"time": 1.0, "x": 5.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
                    {"time": 2.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
                ],
            }
        ]
    }


def _quintic_smoothed_choreo(rate_hz: float = 20.0, duration: float = 12.0):
    """A single drone flown along a quintic-smoothed P0->P1->P0 transition,
    densely sampled so the piecewise-linear waypoint interpolation closely
    tracks the smooth analytic curve (low discretization jerk).
    """
    p0 = (0.0, 0.0, -2.0)
    p1 = (5.0, 0.0, -2.0)
    half = duration / 2
    dt = 1.0 / rate_hz
    waypoints = []
    t = 0.0
    while t <= duration + 1e-9:
        if t <= half:
            state = quintic_segment(p0, p1, half, t)
        else:
            state = quintic_segment(p1, p0, half, t - half)
        x, y, z = state["position"]
        waypoints.append({"time": round(t, 4), "x": x, "y": y, "z": z, "yaw": 0.0})
        t += dt
    return {"drones": [{"id": 0, "waypoints": waypoints}]}


class TestJerkViolationDetection:
    def test_sharp_reversal_is_flagged(self):
        report = validate_dynamics(_sharp_reversal_choreo())
        assert report["violations"]["jerk"], "expected the velocity reversal to trigger a jerk violation"
        assert report["status"] == "FAILED"
        assert report["feasibility_certified"] is False

    def test_quintic_smoothed_transition_has_no_jerk_violations(self):
        report = validate_dynamics(_quintic_smoothed_choreo())
        assert report["violations"]["jerk"] == []
        assert report["metrics"]["max_jerk_m_s3"] <= DynamicLimits().max_jerk_ms3

    def test_max_jerk_metric_is_reported(self):
        report = validate_dynamics(_sharp_reversal_choreo())
        assert report["metrics"]["max_jerk_m_s3"] > DynamicLimits().max_jerk_ms3

    def test_tighter_jerk_limit_can_fail_an_otherwise_smooth_show(self):
        report = validate_dynamics(
            _quintic_smoothed_choreo(), limits=DynamicLimits(max_jerk_ms3=0.001)
        )
        assert report["violations"]["jerk"]
        assert report["status"] == "FAILED"


class TestCertifyFeasibility:
    def test_clean_show_is_certified(self):
        assert certify_feasibility(_quintic_smoothed_choreo()) is True

    def test_jerky_show_is_not_certified(self):
        assert certify_feasibility(_sharp_reversal_choreo()) is False

    def test_no_drones_is_not_certified(self):
        assert certify_feasibility({"drones": []}) is False
