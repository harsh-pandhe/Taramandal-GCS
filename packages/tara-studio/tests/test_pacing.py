"""Coverage for drone-count-aware show pacing (issue #11).

The generators used to run every show on one hardcoded timeline — 5 s takeoff,
2 s blend, 20 s choreography, whatever was left for landing — no matter how
many drones were flying. The distances each phase has to cover grow with the
fleet size, so the speed those fixed windows implied grew with it too, and a
large show failed the velocity/acceleration checks on arrival.

What makes it a generator bug rather than an operator inconvenience is that the
phase boundaries were literal constants, so the operator could not stretch the
timeline to fix it either: raising ``duration`` only lengthened the landing
window. ``test_raising_duration_alone_never_fixed_the_speed`` pins that.
"""

import math

import pytest

from tara_core import DEFAULT_LIMITS
from tara_studio.generator import (
    generate_circle,
    generate_cube,
    generate_heart,
    generate_helix,
    generate_spline,
    plan_show_pacing,
)
from tara_studio.pacing import (
    BASE_CHOREO_S,
    BASE_LAND_S,
    BASE_TAKEOFF_S,
    plan_pacing,
)
from tara_studio.validator import validate_choreography

ALL_SHAPES = ["circle", "helix", "heart", "cube", "spline"]
GENERATORS = {
    "circle": generate_circle,
    "helix": generate_helix,
    "heart": generate_heart,
    "cube": generate_cube,
    "spline": generate_spline,
}


def _show_length(choreo: dict) -> float:
    return max(d["waypoints"][-1]["time"] for d in choreo["drones"])


def _peak(choreo: dict, metric: str) -> float:
    return validate_choreography(choreo)["metrics"][metric]


class TestPacingScalesWithDroneCount:
    """The acceptance criterion: pacing is computed from the drone count, and
    it actually changes between drone counts."""

    @pytest.mark.parametrize("shape", ALL_SHAPES)
    def test_pacing_multiplier_grows_with_fleet_size(self, shape):
        # Two drone counts, as the issue asks for, plus the ones between them
        # so a non-monotonic multiplier (a pacing rule that slows down and then
        # speeds back up as n grows) can't slip through.
        multipliers = [plan_show_pacing(n, shape).multiplier for n in (3, 10, 20, 50, 100)]

        assert multipliers == sorted(multipliers), (
            f"{shape} pacing must never speed up as the fleet grows: {multipliers}"
        )
        assert multipliers[-1] > multipliers[0], (
            f"{shape} at 100 drones is paced no slower than at 3: {multipliers}"
        )

    @pytest.mark.parametrize("shape", ALL_SHAPES)
    def test_generated_show_is_actually_longer_at_100_than_at_10(self, shape):
        # The multiplier has to reach the waypoints, not just the plan.
        short = _show_length(GENERATORS[shape](10, 30.0, 2.0))
        long = _show_length(GENERATORS[shape](100, 30.0, 2.0))

        assert long > short, f"{shape}: N=100 show ({long}s) is not longer than N=10 ({short}s)"

    def test_pacing_responds_to_geometry_not_just_the_count(self):
        # Pacing is derived from the distances a show must cover. A fleet that
        # has to climb twice as high needs a longer takeoff at the same n.
        low = plan_pacing(50, climb_m=10.0, spread_m=20.0)
        high = plan_pacing(50, climb_m=100.0, spread_m=20.0)

        assert high.takeoff_s > low.takeoff_s


class TestSmallShowsAreNotRegressed:
    """'Existing small-N shows aren't paced slower than necessary.'

    The historical timeline is the floor, not a target to scale from, so a
    fleet small enough to fly it safely still flies it.
    """

    @pytest.mark.parametrize("shape", ALL_SHAPES)
    def test_tiny_fleets_stay_within_a_hair_of_the_historical_timeline(self, shape):
        # A single drone adds no fleet-driven distance at all, so pacing has
        # essentially nothing to stretch. The phases do not come out *exactly*
        # at their historical values for every shape, because two of them carry
        # a shape-specific cost that was always there and was always slightly
        # over budget on the fixed timeline: the helix orbits at radius 5 (an
        # 11 m return leg, which needs 6.1 s rather than 5 s) and spins 1.5x
        # (which needs 21.7 s rather than 20 s to stay under the yaw ceiling).
        # What matters for the no-regression criterion is that nothing is
        # stretched *materially*, and nothing is ever shortened.
        pacing = plan_show_pacing(1, shape)

        # Issue #60 replaced generator._ease's smoothstep ramp (peak = 1.5x
        # mean) with tara_core.geometry.quintic_profile's minimum-jerk ramp
        # (peak = 1.875x mean, needed so acceleration is also continuous at
        # phase boundaries, not just velocity). To keep the same peak-speed
        # margin under the ceiling at scale, pacing.CRUISE_FRACTION was cut by
        # the matching 1.5/1.875 factor (0.5 -> 0.4), which stretches any
        # cruise-bound (non-floored) phase by the inverse, 1.25x. Helix's land
        # phase was already the closest to its floor of any shape (see the
        # docstring above on its 11 m return leg), so it's the one case that
        # 1.25x factor pushes past the old tolerance bands; both bounds below
        # are widened by that same, derived factor rather than loosened
        # arbitrarily.
        assert pacing.takeoff_s == pytest.approx(BASE_TAKEOFF_S)
        assert BASE_CHOREO_S <= pacing.choreo_s <= BASE_CHOREO_S * 1.15
        assert BASE_LAND_S <= pacing.land_s <= BASE_LAND_S * 1.25 * 1.25
        assert pacing.multiplier < 1.15 * 1.25

    @pytest.mark.parametrize("shape", ALL_SHAPES)
    def test_three_drone_show_is_not_meaningfully_slowed(self, shape):
        # The N=3 show was already safe on the fixed timeline, so pacing has
        # nothing to buy there and must not spend show time on it anyway.
        # Upper bound widened by the same 1.25x factor as the N=1 test above,
        # for the same reason (issue #60's quintic recalibration); the lower
        # bound is unaffected, per test_pacing_never_returns_a_multiplier_below_one.
        multiplier = plan_show_pacing(3, shape).multiplier
        assert 1.0 <= multiplier <= 1.0 + 0.15 * 1.25

    def test_ten_drone_case_is_not_regressed_into_a_crawl(self):
        # The issue calls out N=10 specifically. It does need *some* stretching
        # (its climb and blend genuinely exceed the fixed windows), but it
        # should stay in the same order of magnitude, not balloon.
        assert plan_show_pacing(10, "circle").multiplier < 2.0

    @pytest.mark.parametrize("shape", ALL_SHAPES)
    def test_pacing_never_returns_a_multiplier_below_one(self, shape):
        # A multiplier under 1.0 would mean pacing had made a show *faster*
        # than the timeline that was already validated as flyable.
        for n in (1, 2, 3, 5, 10, 25, 60, 100):
            assert plan_show_pacing(n, shape).multiplier >= 1.0


class TestLargeShowsPassTheSpeedChecks:
    """'A freshly generated show at N=100 passes safety validation without
    manual timeline adjustment.'

    Scoped to the checks pacing governs — velocity, acceleration and yaw rate.
    Proximity is a property of the formation's geometry rather than its
    timing: 100 drones on the circle's fixed 4 m radius sit 0.25 m apart no
    matter how slowly they fly it. Resizing the formations is a separate
    concern (and cannot be done without pushing them past the geofence), so it
    is not asserted here — see ``TestProximityIsNotAPacingProblem``.
    """

    @pytest.mark.parametrize("shape", ALL_SHAPES)
    @pytest.mark.parametrize("n", [50, 100])
    def test_speed_limits_are_respected_at_scale(self, shape, n):
        report = validate_choreography(GENERATORS[shape](n, 30.0, 2.0))

        assert report["violations"]["velocity"] == []
        assert report["violations"]["acceleration"] == []
        assert report["violations"]["yaw_rate"] == []

    @pytest.mark.parametrize("shape", ALL_SHAPES)
    def test_peak_speed_stays_under_the_ceiling_at_100_drones(self, shape):
        metrics = validate_choreography(GENERATORS[shape](100, 30.0, 2.0))["metrics"]

        assert metrics["max_velocity_m_s"] <= DEFAULT_LIMITS.max_velocity_ms
        assert metrics["max_acceleration_m_s2"] <= DEFAULT_LIMITS.max_accel_ms2
        assert metrics["max_yaw_rate_deg_s"] <= DEFAULT_LIMITS.max_yaw_rate_dps

    def test_the_100_drone_case_used_to_fail_by_an_order_of_magnitude(self):
        # Pins the size of the win: peak speed at N=100 was 60 m/s against a
        # 6 m/s ceiling before pacing. A regression that quietly reintroduced
        # fixed pacing would show up here first.
        assert _peak(generate_circle(100, 30.0, 2.0), "max_velocity_m_s") < 6.0


class TestPacingCannotBeWorkedAroundManually:
    """Why this had to be fixed in the generator rather than left to the operator."""

    def test_raising_duration_alone_never_fixed_the_speed(self):
        # Before pacing, the phase boundaries were the literals 5.0 and 25.0,
        # so a longer `duration` only stretched the landing window and peak
        # speed stayed pinned at ~51 m/s. Now `duration` is a floor on the
        # whole timeline, and the pacing floor is what actually governs — so a
        # 100-drone show is safe at the *default* duration, with no manual
        # stretching at all.
        default_duration = validate_choreography(generate_circle(100, 30.0, 2.0))
        assert default_duration["violations"]["velocity"] == []

    def test_a_longer_requested_duration_is_still_honoured(self):
        # Pacing sets a floor, it does not override an operator asking for a
        # longer show: a 3-drone show asked for 90 s still runs about 90 s.
        assert _show_length(generate_circle(3, 90.0, 2.0)) == pytest.approx(90.0, abs=2.0)

    def test_a_too_short_duration_is_raised_to_what_the_fleet_can_fly(self):
        # The converse: asking 100 drones to finish in 30 s cannot be honoured
        # safely, so the generator returns the longer show it actually needs
        # rather than a fast one that fails validation.
        assert _show_length(generate_circle(100, 30.0, 2.0)) > 30.0


class TestPhaseTransitionsAreContinuous:
    """Discontinuities at phase boundaries are the one speed problem extra show
    time cannot fix — a step between two adjacent samples has the same size
    however far apart in seconds those samples are. They are pacing's business
    because they are what stopped a stretched timeline from ever validating.
    """

    @pytest.mark.parametrize("shape", ALL_SHAPES)
    def test_no_position_teleports_between_adjacent_waypoints(self, shape):
        rate = 2.0
        # At the 6 m/s ceiling a 0.5 s step covers 3 m; allow a little over that
        # for the eased peak, but nothing like a phase-boundary jump.
        max_step_m = DEFAULT_LIMITS.max_velocity_ms / rate * 1.2

        for drone in GENERATORS[shape](20, 30.0, rate)["drones"]:
            wps = drone["waypoints"]
            for prev, cur in zip(wps, wps[1:]):
                step = math.dist(
                    (prev["x"], prev["y"], prev["z"]), (cur["x"], cur["y"], cur["z"])
                )
                assert step <= max_step_m, (
                    f"{shape} drone {drone['id']} jumps {step:.2f} m between "
                    f"t={prev['time']} and t={cur['time']}"
                )

    @pytest.mark.parametrize("shape", ALL_SHAPES)
    def test_no_heading_flips_between_adjacent_waypoints(self, shape):
        rate = 2.0
        max_step_deg = DEFAULT_LIMITS.max_yaw_rate_dps / rate

        for drone in GENERATORS[shape](20, 30.0, rate)["drones"]:
            wps = drone["waypoints"]
            for prev, cur in zip(wps, wps[1:]):
                delta = abs((cur["yaw"] - prev["yaw"] + 180.0) % 360.0 - 180.0)
                assert delta <= max_step_deg, (
                    f"{shape} drone {drone['id']} yaw jumps {delta:.1f} deg between "
                    f"t={prev['time']} and t={cur['time']}"
                )


class TestProximityIsNotAPacingProblem:
    """Documents, as executable fact, why the N=100 proximity failures are out
    of this issue's scope: the formations are statically too small for the
    fleet, and no timeline changes that.
    """

    def test_the_circle_formation_is_geometrically_too_small_at_100_drones(self):
        radius = 4.0  # the circle's fixed radius
        neighbour_gap = 2.0 * radius * math.sin(math.pi / 100)

        assert neighbour_gap < DEFAULT_LIMITS.design_separation_m
        # Fixing it would need a radius this large...
        needed = DEFAULT_LIMITS.design_separation_m / (2.0 * math.sin(math.pi / 100))
        # ...which does not fit inside the geofence the validator enforces, so
        # it cannot be fixed by resizing the formation either. That makes it a
        # formation-design question, not a pacing one.
        assert needed > DEFAULT_LIMITS.geofence_radius_m

    def test_pacing_does_not_pretend_to_fix_separation(self):
        # Slowing a show down leaves its minimum separation untouched, which is
        # the cleanest statement of why proximity is a separate concern.
        fast = validate_choreography(generate_circle(100, 30.0, 2.0))
        slow = validate_choreography(generate_circle(100, 120.0, 2.0))

        assert fast["metrics"]["min_distance_m"] == pytest.approx(
            slow["metrics"]["min_distance_m"], abs=0.35
        )


class TestPacingApiSurface:
    def test_plan_show_pacing_matches_what_the_generator_produces(self):
        pacing = plan_show_pacing(50, "circle")
        actual = _show_length(generate_circle(50, 30.0, 2.0))

        assert actual == pytest.approx(pacing.total_s, abs=1.0)

    def test_unknown_shape_is_rejected_by_name(self):
        with pytest.raises(ValueError, match="rhombus"):
            plan_show_pacing(10, "rhombus")

    @pytest.mark.parametrize("bad", [0, -1, 100_001])
    def test_out_of_range_fleet_sizes_are_rejected(self, bad):
        with pytest.raises(ValueError, match="num_drones"):
            plan_pacing(bad, climb_m=10.0, spread_m=10.0)

    def test_negative_distances_are_rejected_by_name(self):
        with pytest.raises(ValueError, match="climb_m"):
            plan_pacing(10, climb_m=-1.0, spread_m=10.0)

    def test_phases_sum_to_the_reported_total(self):
        pacing = plan_show_pacing(40, "helix")

        assert pacing.total_s == pytest.approx(
            pacing.takeoff_s + pacing.choreo_s + pacing.land_s
        )
        assert pacing.land_start_s == pytest.approx(pacing.takeoff_s + pacing.choreo_s)
