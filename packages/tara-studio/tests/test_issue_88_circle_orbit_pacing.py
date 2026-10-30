"""Issue #88: give the circle's orbit an explicit time budget in `plan_pacing`.

Before this fix, `plan_pacing`'s choreography window was sized from climb,
blend and yaw-rate alone. A ring's rotation is bounded by *tangential
velocity* around a real-metre radius, not yaw rate, so `generator._circle_sweep`
worked around the gap after the fact: it measured how much of a turn the
ring's outer radius could make within the window `plan_pacing` already
returned, and clipped the *sweep* to fit rather than growing the window to
fit the rotation. At N=75/90/100 a large ring completed only ~1/3 of a turn
(measured directly against this file's own pre-fix run: sweep_rad of 2.09,
2.12 and 2.14 respectively, vs a full turn at 2*pi = 6.28).

`plan_pacing`'s new `orbit_reach_m` parameter floors the window itself for a
full orbit whenever one is physically feasible, the same way `climb_m` and
`spread_m` already floor it for climb and blend. `_circle_sweep` keeps its
own clamp as the fallback for the one case the floor can't fix: the blend's
own peak speed alone already consuming the velocity budget.
"""

from __future__ import annotations

import math

import pytest

from tara_core.show import Show
from tara_studio.generator import (
    _circle_reach_m,
    _circle_sweep,
    _launch_state,
    _pacing_for,
    _SHAPES,
    generate_circle,
    generate_cube,
    generate_heart,
    generate_helix,
    generate_spline,
)
from tara_studio.pacing import plan_pacing
from tara_studio.validator import validate_choreography

DURATION = 30.0
RATE = 2.0
FULL_TURN = 2.0 * math.pi


def _circle_pacing(n: int):
    start_positions, heights = _launch_state(n, None, None)
    return _pacing_for(_SHAPES["circle"], n, start_positions, heights)


class TestPlanPacingOrbitReachM:
    """Unit coverage of the new parameter in isolation from the generator."""

    def test_zero_is_the_default_and_a_no_op(self):
        with_zero = plan_pacing(50, climb_m=10.0, spread_m=20.0, revolutions=1.0, orbit_reach_m=0.0)
        omitted = plan_pacing(50, climb_m=10.0, spread_m=20.0, revolutions=1.0)
        assert with_zero == omitted

    def test_negative_orbit_reach_m_rejected(self):
        with pytest.raises(ValueError):
            plan_pacing(10, climb_m=10.0, spread_m=10.0, orbit_reach_m=-1.0)

    def test_large_reach_extends_the_floor_like_climb_and_spread_do(self):
        baseline = plan_pacing(50, climb_m=10.0, spread_m=20.0, revolutions=1.0, orbit_reach_m=0.0)
        widened = plan_pacing(50, climb_m=10.0, spread_m=20.0, revolutions=1.0, orbit_reach_m=50.0)
        assert widened.choreo_s > baseline.choreo_s
        # Only choreo_s should move — orbit_reach_m is not a climb/blend/land input.
        assert widened.takeoff_s == baseline.takeoff_s
        assert widened.blend_s == baseline.blend_s
        assert widened.land_s == baseline.land_s

    def test_small_reach_never_shrinks_below_the_other_floors(self):
        baseline = plan_pacing(3, climb_m=1.0, spread_m=1.0, revolutions=1.0, orbit_reach_m=0.0)
        widened = plan_pacing(3, climb_m=1.0, spread_m=1.0, revolutions=1.0, orbit_reach_m=0.1)
        assert widened.choreo_s == baseline.choreo_s


class TestCircleSweepSmallNUnchanged:
    """Pins the exact pre-fix values (measured on main before this change)
    for the small fleet sizes the issue says must not move — orbit_reach_m
    only ever raises choreo_s via max(), and small rings' orbit_s floor is
    already under the blend/yaw floors, so nothing here should differ from
    before the fix."""

    @pytest.mark.parametrize(
        "n,expected_choreo_s",
        [(3, 20.0), (10, 24.6847755070504)],
    )
    def test_choreo_s_pinned(self, n, expected_choreo_s):
        assert _circle_pacing(n).choreo_s == pytest.approx(expected_choreo_s)

    @pytest.mark.parametrize("n", [3, 10])
    def test_sweep_already_was_a_full_turn(self, n):
        # Small rings always fit a full turn in the base window — the bug
        # only shows up once the ring outgrows it (N=50+ below).
        assert _circle_sweep(n) == pytest.approx(FULL_TURN)


class TestCircleSweepLargeNNowCompletesAFullTurn:
    """The actual fix: N=50/75/90/100 (the exact sizes #73's own review
    measured the pre-fix gap at) now complete, or nearly complete, a full
    orbit instead of clipping to ~1/3 of one."""

    @pytest.mark.parametrize("n", [50, 75, 90, 100])
    def test_sweep_is_a_full_turn(self, n):
        assert _circle_sweep(n) == pytest.approx(FULL_TURN, abs=1e-6)

    @pytest.mark.parametrize("n", [50, 75, 90, 100])
    def test_choreo_s_floored_by_the_orbit_budget(self, n):
        """The window must actually be the one driving the full turn, not a
        coincidence of some other floor already being just as large."""
        with_orbit = _circle_pacing(n).choreo_s
        reach = _circle_reach_m(n)
        start_positions, heights = _launch_state(n, None, None)
        no_orbit = plan_pacing(
            n,
            climb_m=max(heights.values()),
            spread_m=0.0,  # irrelevant to this comparison; isolate the orbit term
            revolutions=1.0,
            orbit_reach_m=0.0,
        ).choreo_s
        assert with_orbit > no_orbit
        assert reach > 0


class TestNoOtherShapeIsAffected:
    """Acceptance: "no regression to helix/heart/other formation regression
    tests" — every shape but circle must still default orbit_reach_fn to
    None, i.e. contribute exactly 0.0 to its own pacing, same as before this
    issue existed."""

    @pytest.mark.parametrize("shape_name", ["helix", "heart", "cube", "spline"])
    def test_orbit_reach_fn_is_none(self, shape_name):
        assert _SHAPES[shape_name].orbit_reach_fn is None


@pytest.mark.parametrize("n", [50, 75, 90, 100])
def test_large_circle_show_still_passes_full_dynamics_validation(n):
    """The real safety regression check: a longer choreography window and a
    now-uncapped sweep must not introduce any velocity/acceleration/jerk/
    proximity violation across the whole built show."""
    choreo = generate_circle(n, DURATION, RATE)
    report = validate_choreography(choreo)
    assert report["status"] == "PASSED", report["violations"]


@pytest.mark.parametrize(
    "shape_name,generate",
    [
        ("helix", generate_helix),
        ("heart", generate_heart),
        ("cube", generate_cube),
        ("spline", generate_spline),
    ],
)
def test_other_shapes_still_pass_at_n_100(shape_name, generate):
    """Direct regression guard alongside circle's own N=50/75/90/100 sweep
    above, at the same N test_generator_regression.py already covers."""
    choreo = generate(100, DURATION, RATE)
    report = validate_choreography(choreo)
    assert report["status"] == "PASSED", (shape_name, report["violations"])


def test_predicted_duration_matches_actual_built_duration_at_n_100():
    """Acceptance: "pacing.multiplier/duration prediction still equals the
    actual built duration" — plan_pacing's total_s must match the real
    show's last waypoint time, not just agree with itself in isolation."""
    n = 100
    predicted = _circle_pacing(n).total_s
    choreo = generate_circle(n, DURATION, RATE)
    show = Show.from_any(choreo)
    assert show.duration == pytest.approx(predicted, abs=1.0 / RATE)
