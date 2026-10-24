"""Issue #60: minimum-jerk quintic profile and jerk-from-positions correctness."""

from __future__ import annotations

import math

import pytest

from tara_core.geometry import jerk_from_positions, quintic_profile, quintic_segment


class TestQuinticProfile:
    def test_endpoints_are_at_rest(self):
        pos0, vel0, acc0, _jerk0 = quintic_profile(0.0)
        pos1, vel1, acc1, _jerk1 = quintic_profile(1.0)
        assert pos0 == pytest.approx(0.0)
        assert vel0 == pytest.approx(0.0)
        assert acc0 == pytest.approx(0.0)
        assert pos1 == pytest.approx(1.0)
        assert vel1 == pytest.approx(0.0)
        assert acc1 == pytest.approx(0.0)

    def test_peak_velocity_is_1_875x_the_mean(self):
        # The mean rate over the unit ramp is exactly 1 (position goes 0->1
        # over ratio 1), so the midpoint velocity *is* the peak/mean ratio.
        _pos, vel, _acc, _jerk = quintic_profile(0.5)
        assert vel == pytest.approx(1.875)

    def test_monotonic_and_clamped_outside_0_1(self):
        below = quintic_profile(-0.5)
        above = quintic_profile(1.5)
        assert below == quintic_profile(0.0)
        assert above == quintic_profile(1.0)

    def test_position_is_monotonically_increasing(self):
        ratios = [i / 100 for i in range(101)]
        positions = [quintic_profile(r)[0] for r in ratios]
        assert positions == sorted(positions)


class TestQuinticSegment:
    def test_endpoints_match_p0_p1_at_rest(self):
        p0, p1 = (0.0, 0.0, 0.0), (10.0, 4.0, 6.0)
        start = quintic_segment(p0, p1, duration=2.0, t=0.0)
        end = quintic_segment(p0, p1, duration=2.0, t=2.0)

        assert start["position"] == pytest.approx(p0)
        assert start["velocity"] == pytest.approx((0.0, 0.0, 0.0))
        assert start["acceleration"] == pytest.approx((0.0, 0.0, 0.0))
        assert end["position"] == pytest.approx(p1)
        assert end["velocity"] == pytest.approx((0.0, 0.0, 0.0))
        assert end["acceleration"] == pytest.approx((0.0, 0.0, 0.0))

    def test_midpoint_velocity_matches_the_scaled_analytic_peak(self):
        p0, p1 = (0.0, 0.0, 0.0), (10.0, 0.0, 0.0)
        duration = 2.0
        mid = quintic_segment(p0, p1, duration, t=1.0)
        # peak = delta * 1.875 / duration
        assert mid["velocity"][0] == pytest.approx(10.0 * 1.875 / duration)

    def test_zero_duration_collapses_to_p1_at_rest(self):
        p0, p1 = (0.0, 0.0, 0.0), (5.0, 5.0, 5.0)
        state = quintic_segment(p0, p1, duration=0.0, t=0.0)
        assert state["position"] == p1
        assert state["velocity"] == (0.0, 0.0, 0.0)
        assert state["jerk"] == (0.0, 0.0, 0.0)


class TestJerkFromPositions:
    def test_zero_for_constant_velocity(self):
        # Evenly spaced points along a straight line at constant speed have
        # zero jerk (and zero acceleration) by construction.
        dt = 0.1
        pts = [(i * dt * 2.0, 0.0, 0.0) for i in range(-2, 3)]
        j = jerk_from_positions(pts[0], pts[1], pts[3], pts[4], dt)
        assert j == pytest.approx(0.0, abs=1e-9)

    def test_matches_known_cubic_motion(self):
        # x(t) = t^3 has x'''(t) = 6 exactly, constant everywhere, so the
        # central stencil should recover it to within its own truncation
        # error (which shrinks with dt, so a small dt should match tightly).
        dt = 0.01
        t0 = 1.0

        def x(t):
            return t**3

        pts = [(x(t0 + k * dt), 0.0, 0.0) for k in (-2, -1, 1, 2)]
        j = jerk_from_positions(*pts, dt)
        assert j == pytest.approx(6.0, rel=1e-3)

    def test_matches_quintic_segment_analytic_jerk(self):
        # Cross-check: sampling quintic_segment's own position output and
        # differentiating numerically should recover its analytic jerk.
        p0, p1 = (0.0, 0.0, 0.0), (10.0, 0.0, 0.0)
        duration = 2.0
        dt = 0.001
        t_mid = duration / 2

        pts = [
            quintic_segment(p0, p1, duration, t_mid + k * dt)["position"]
            for k in (-2, -1, 1, 2)
        ]
        numeric_jerk = jerk_from_positions(*pts, dt)
        analytic_jerk = math.dist(
            (0.0, 0.0, 0.0), quintic_segment(p0, p1, duration, t_mid)["jerk"]
        )
        assert numeric_jerk == pytest.approx(analytic_jerk, rel=1e-2)


def _linear_reference(waypoints, t):
    """The pre-#156 linear-scan implementation, kept as an equivalence oracle."""
    if not waypoints:
        return None
    if t <= waypoints[0]["time"]:
        return waypoints[0]
    if t >= waypoints[-1]["time"]:
        return waypoints[-1]
    for w1, w2 in zip(waypoints, waypoints[1:]):
        if w1["time"] <= t <= w2["time"]:
            span = w2["time"] - w1["time"]
            ratio = (t - w1["time"]) / span if span > 0 else 0.0
            yaw1, yaw2 = w1.get("yaw", 0.0), w2.get("yaw", 0.0)
            diff = (yaw2 - yaw1) % 360
            if diff > 180:
                diff -= 360
            return {
                "time": t,
                "x": w1["x"] + ratio * (w2["x"] - w1["x"]),
                "y": w1["y"] + ratio * (w2["y"] - w1["y"]),
                "z": w1["z"] + ratio * (w2["z"] - w1["z"]),
                "yaw": (yaw1 + ratio * diff) % 360,
                "vx": (w2["x"] - w1["x"]) / span if span > 0 else 0.0,
                "vy": (w2["y"] - w1["y"]) / span if span > 0 else 0.0,
                "vz": (w2["z"] - w1["z"]) / span if span > 0 else 0.0,
            }
    return waypoints[-1]


def test_bisect_interpolation_matches_linear_scan_including_duplicate_times():
    import random

    from tara_core.geometry import interpolate_waypoint

    rng = random.Random(156)
    for _ in range(200):
        times = sorted(rng.choice([0.0, 0.5, 1.0, 1.0, 2.0, 3.5, 3.5, 7.0]) + rng.random() * (rng.random() < 0.5) for _ in range(rng.randint(1, 12)))
        wps = [
            {"time": tm, "x": rng.uniform(-9, 9), "y": rng.uniform(-9, 9), "z": rng.uniform(0, 100), "yaw": rng.choice([0, 90, 350, 10, 180])}
            for tm in times
        ]
        probes = list(times) + [rng.uniform(-1, times[-1] + 1) for _ in range(30)]
        for t in probes:
            assert interpolate_waypoint(wps, t) == _linear_reference(wps, t), (times, t)


def test_interpolation_scales_to_long_trajectories():
    import time

    from tara_core.geometry import interpolate_waypoint

    wps = [{"time": float(i), "x": float(i), "y": 0.0, "z": 10.0, "yaw": 0.0} for i in range(20001)]
    start = time.perf_counter()
    for k in range(20000):
        interpolate_waypoint(wps, k + 0.5)
    # Linear scan needs ~2e8 comparisons here (tens of seconds); bisect is milliseconds.
    assert time.perf_counter() - start < 2.0
