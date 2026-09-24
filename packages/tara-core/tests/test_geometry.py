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
