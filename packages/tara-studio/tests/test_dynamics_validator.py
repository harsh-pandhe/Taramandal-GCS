"""Issue #60: jerk detection, DynamicLimits.max_jerk_ms3, and certify_feasibility."""

from __future__ import annotations

import math

import pytest

from tara_core.geometry import quintic_segment
from tara_core.validate import MAX_REPORTED, report_subset
from tara_studio import validator
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


# --- Issue #152: a late worst violation must not be cut by the report cap ---------------------------------------


def _line_show(segments: list[tuple[float, float]]) -> dict:
    """One drone on the X axis: ``(duration_s, speed_m_s)`` segments back to back."""
    t, x = 0.0, 0.0
    waypoints = [{"time": t, "x": x, "y": 0.0, "z": -2.0, "yaw": 0.0}]
    for duration, speed in segments:
        t, x = t + duration, x + duration * speed
        waypoints.append({"time": t, "x": x, "y": 0.0, "z": -2.0, "yaw": 0.0})
    return {"drones": [{"id": 0, "waypoints": waypoints}]}


def _circle_show(phases: list[tuple[float, float]], radius: float = 10.0, dt: float = 0.1) -> dict:
    """One drone on a circle: ``(duration_s, angular_rate_rad_s)`` phases, sampled every ``dt``."""
    theta, t = 0.0, 0.0
    waypoints = []
    for duration, omega in phases:
        for _ in range(round(duration / dt)):
            waypoints.append({
                "time": round(t, 4), "x": radius * math.cos(theta), "y": radius * math.sin(theta),
                "z": -2.0, "yaw": 0.0,
            })
            theta += omega * dt
            t += dt
    return {"drones": [{"id": 0, "waypoints": waypoints}]}


def _separation_show() -> dict:
    """Two drones 1.4 m apart (just inside the 1.5 m limit) for 10 s, then closing to 0.2 m."""
    def drone(drone_id: int, y_end: float) -> dict:
        y0 = 1.4 * drone_id
        return {"id": drone_id, "waypoints": [
            {"time": 0.0, "x": 0.0, "y": y0, "z": -2.0, "yaw": 0.0},
            {"time": 10.0, "x": 0.0, "y": y0, "z": -2.0, "yaw": 0.0},
            {"time": 11.0, "x": 0.0, "y": y_end, "z": -2.0, "yaw": 0.0},
            {"time": 12.0, "x": 0.0, "y": y_end, "z": -2.0, "yaw": 0.0},
        ]}
    return {"drones": [drone(0, 0.0), drone(1, 0.2)]}


#: category -> (show with >50 early violations and a worse one at the end, field holding the measured value)
LATE_WORST_SHOWS = {
    "velocity": (lambda: _line_show([(10.0, 5.0), (1.0, 20.0)]), "velocity"),
    "centripetal": (lambda: _circle_show([(10.0, 0.6), (2.0, 1.2)]), "centripetal"),
    "jerk": (lambda: _circle_show([(10.0, 1.4), (2.0, 2.0)]), "jerk"),
    "separation": (_separation_show, "distance"),
}


@pytest.fixture
def report_cap(monkeypatch):
    """Run validate_dynamics with a different report cap than the default."""
    def set_cap(cap: int) -> None:
        monkeypatch.setattr(validator, "report_subset", lambda found: report_subset(found, cap=cap))
    return set_cap


class TestWorstViolationSurvivesTruncation:
    @pytest.mark.parametrize("category", sorted(LATE_WORST_SHOWS))
    def test_a_late_worst_violation_is_reported(self, category):
        build, field = LATE_WORST_SHOWS[category]
        report = validate_dynamics(build())

        reported = report["violations"][category]
        worst = report["worst_violations"][category]
        assert len(reported) == MAX_REPORTED, "the show must overflow the cap for this test to mean anything"
        assert worst in reported
        # The worst moment comes after the 50th violation by time, so the old
        # first-50-by-time truncation would have dropped it.
        assert worst["time"] > 9.0
        others = [v[field] for v in reported]
        if category == "separation":
            assert worst[field] == min(others)
        else:
            assert worst[field] == max(others)
        assert [v["time"] for v in reported] == sorted(v["time"] for v in reported)

    @pytest.mark.parametrize("category", sorted(LATE_WORST_SHOWS))
    @pytest.mark.parametrize("cap", [10, 200])
    def test_the_cap_never_changes_the_verdict(self, category, cap, report_cap):
        show = LATE_WORST_SHOWS[category][0]()
        baseline = validate_dynamics(show)
        report_cap(cap)
        capped = validate_dynamics(show)

        for key in ("status", "metrics", "summary", "feasibility_certified"):
            assert capped[key] == baseline[key]
        for key, entries in capped["violations"].items():
            assert len(entries) <= cap
            # Truncation never empties a category that has violations.
            assert bool(entries) == bool(baseline["violations"][key])
        assert capped["worst_violations"] == baseline["worst_violations"]
        assert capped["worst_violations"][category] in capped["violations"][category]

    def test_a_clean_show_has_no_worst_violations(self):
        report = validate_dynamics(_quintic_smoothed_choreo())
        assert report["status"] == "PASSED"
        assert set(report["worst_violations"]) == set(report["violations"])
        assert all(w is None for w in report["worst_violations"].values())

    def test_an_empty_show_has_no_worst_violations(self):
        report = validate_dynamics({"drones": []})
        assert report["worst_violations"] == {
            "velocity": None, "centripetal": None, "jerk": None, "separation": None,
        }

    def test_worst_is_ranked_on_unrounded_speed(self):
        """4.002 and 4.004 m/s both display as 4.0, but 4.004 is worse (and later)."""
        report = validate_dynamics(_line_show([(5.0, 4.002), (5.0, 4.004)]))
        worst = report["worst_violations"]["velocity"]
        assert worst["velocity"] == 4.0
        assert worst["time"] > 5.0
