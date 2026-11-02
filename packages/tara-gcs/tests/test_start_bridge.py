"""The transition from the formation hold to a show's first waypoint (issue #172, F3).

The gate leaves every drone hovering at its altitude layer (2, 4, 6 ... m), but
a Studio show begins on the pad (z = 0) with its own takeoff leg. The streamed
loop used to command the show's first point on the very first tick, so every
drone was sent straight down to its pad: on SITL drone 0 went from 1.9 m to
0.7 m in about 1.3 s, and drone 9 of 10 took about 9.8 s to settle. These tests
pin the leg that replaces that step - and the separation check that guards it.
"""

import math

import pytest

from tara_core import DEFAULT_LIMITS, altitude_layers
from tara_core.layout import launch_grid
from tara_core.geometry import interpolate_waypoint
from tara_gcs.drone_manager import DroneManager
from tara_gcs.start_bridge import (
    BRIDGE_PEAK_SPEED_MS,
    BRIDGE_TOLERANCE_M,
    StartBridgeError,
    build_start_bridge,
)


def _show_from_pad(n: int, spacing: float = 2.2) -> dict:
    """Drone i starts on its pad (fleet y = east), climbs to 3 m, and stays."""
    return {
        i: [
            {"time": 0.0, "x": 0.0, "y": spacing * i, "z": 0.0, "yaw": 0.0},
            {"time": 3.0, "x": 0.0, "y": spacing * i, "z": -3.0, "yaw": 0.0},
            {"time": 6.0, "x": 1.0, "y": spacing * i, "z": -3.0, "yaw": 0.0},
        ]
        for i in range(n)
    }


def _hold(n: int, spacing: float = 2.2) -> dict:
    """Formation hold: over its own pad at 2, 4, 6 ... m (NED z is negative up)."""
    return {i: (0.0, spacing * i, -2.0 * (i + 1)) for i in range(n)}


def _sample(plan: dict, drone: int, t: float) -> tuple[float, float, float]:
    p = interpolate_waypoint(plan[drone], t)
    return p["x"], p["y"], p["z"]


def test_a_show_that_already_starts_at_the_hold_is_untouched():
    show = {0: [{"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0}, {"time": 2.0, "x": 1.0, "y": 0.0, "z": -2.0, "yaw": 0.0}]}

    plan, bridge_s = build_start_bridge(show, {0: (0.0, 0.0, -2.0 + BRIDGE_TOLERANCE_M / 2)})

    assert plan is show
    assert bridge_s == 0.0


def test_the_leg_starts_where_the_drone_is_and_hands_over_to_the_show_start():
    show, hold = _show_from_pad(3), _hold(3)

    plan, bridge_s = build_start_bridge(show, hold)

    assert bridge_s > 0.0
    for i in range(3):
        assert _sample(plan, i, 0.0) == pytest.approx(hold[i])  # no step on the first tick
        first = show[i][0]
        assert _sample(plan, i, bridge_s) == pytest.approx((first["x"], first["y"], first["z"]))


def test_the_show_itself_is_unchanged_just_later_by_the_leg_duration():
    show, hold = _show_from_pad(3), _hold(3)

    plan, bridge_s = build_start_bridge(show, hold)

    for i in range(3):
        for t in (0.0, 1.5, 3.0, 4.2, 6.0):
            assert _sample(plan, i, bridge_s + t) == pytest.approx(_sample(show, i, t))


def test_the_leg_never_commands_more_than_the_peak_speed_and_never_below_the_pad():
    """The old behaviour was a position step of up to 6 m in one tick."""
    show, hold = _show_from_pad(3), _hold(3)

    plan, bridge_s = build_start_bridge(show, hold)

    dt = 0.02
    for i in range(3):
        previous = _sample(plan, i, 0.0)
        for k in range(1, int(bridge_s / dt) + 1):
            current = _sample(plan, i, k * dt)
            assert math.dist(previous, current) / dt <= BRIDGE_PEAK_SPEED_MS * 1.05
            assert current[2] <= 1e-9  # NED: never underground
            previous = current


def test_all_drones_share_one_leg_duration_so_the_show_starts_together():
    show, hold = _show_from_pad(3), _hold(3)

    plan, bridge_s = build_start_bridge(show, hold)

    for i in range(3):
        assert plan[i][-1]["time"] - show[i][-1]["time"] == pytest.approx(bridge_s)


def test_the_leg_starts_and_ends_at_rest():
    """Zero velocity at both ends: the show's own first velocity is the only
    discontinuity left, and that is the show's, not the leg's."""
    show, hold = _show_from_pad(1), _hold(1)

    plan, bridge_s = build_start_bridge(show, hold)

    eps = 0.05
    start_speed = math.dist(_sample(plan, 0, 0.0), _sample(plan, 0, eps)) / eps
    end_speed = math.dist(_sample(plan, 0, bridge_s - eps), _sample(plan, 0, bridge_s)) / eps
    assert start_speed < 0.1 * BRIDGE_PEAK_SPEED_MS
    assert end_speed < 0.1 * BRIDGE_PEAK_SPEED_MS


def test_a_drone_with_no_position_refuses_the_start_instead_of_taking_the_old_step():
    """Issue #172 F3, review blocker B-2. A planned drone missing from ``hold``
    used to get no leg and no refusal: its first waypoint was held for the leg
    and then it took the straight step to z = 0 the leg exists to remove."""
    show = _show_from_pad(3)
    hold = {0: _hold(3)[0], 2: _hold(3)[2]}  # drone 1 has no known position

    with pytest.raises(StartBridgeError, match=r"Drone 1\b"):
        build_start_bridge(show, hold)


def test_every_drone_without_a_position_is_named():
    show = _show_from_pad(4)

    with pytest.raises(StartBridgeError) as refused:
        build_start_bridge(show, {0: _hold(4)[0]})

    for drone in (1, 2, 3):
        assert f"Drone {drone}" in str(refused.value)
    assert "Drone 0" not in str(refused.value)


def test_unknown_drone_refuses_start_even_when_known_drones_need_no_bridge():
    """B-2: if known drones are already at their first waypoints, an unknown
    drone must still refuse the start rather than silently skipping the bridge."""
    show = {
        0: [{"time": 0.0, "x": 0.0, "y": 0.0, "z": -5.0, "yaw": 0.0}],
        1: [{"time": 0.0, "x": 3.0, "y": 0.0, "z": -5.0, "yaw": 0.0}],
        2: [{"time": 0.0, "x": 6.0, "y": 0.0, "z": -5.0, "yaw": 0.0}],
    }
    hold = {0: (0.0, 0.0, -5.0), 1: (3.0, 0.0, -5.0)}  # drone 2 unknown

    with pytest.raises(StartBridgeError, match=r"Drone 2\b"):
        build_start_bridge(show, hold)


def test_a_fleet_with_no_known_position_at_all_is_refused_not_stepped():
    """Nothing says the show starts at the hold, so it is not assumed to."""
    with pytest.raises(StartBridgeError, match="Drone 0"):
        build_start_bridge(_show_from_pad(2), {})


def test_a_drone_with_no_waypoints_needs_no_position():
    show = {**_show_from_pad(2), 2: []}

    plan, bridge_s = build_start_bridge(show, {0: _hold(3)[0], 1: _hold(3)[1]})

    assert bridge_s > 0.0
    assert plan[2] == []


def test_the_input_show_is_not_mutated():
    show, hold = _show_from_pad(3), _hold(3)
    before = [[dict(w) for w in wps] for wps in show.values()]

    build_start_bridge(show, hold)

    assert [[dict(w) for w in wps] for wps in show.values()] == before


# --- the separation check: the gate still refuses an unsafe start -------------


def test_a_leg_that_would_bring_two_drones_inside_the_runtime_limit_is_refused():
    """Drone 0 hovers west of drone 1 at the same height, but their first
    waypoints are swapped: the straight legs cross in the middle."""
    hold = {0: (0.0, 0.0, -3.0), 1: (0.0, 6.0, -3.0)}
    show = {
        0: [{"time": 0.0, "x": 0.0, "y": 6.0, "z": -3.0, "yaw": 0.0}, {"time": 2.0, "x": 0.0, "y": 6.0, "z": -3.0, "yaw": 0.0}],
        1: [{"time": 0.0, "x": 0.0, "y": 0.0, "z": -3.0, "yaw": 0.0}, {"time": 2.0, "x": 0.0, "y": 0.0, "z": -3.0, "yaw": 0.0}],
    }

    with pytest.raises(StartBridgeError, match=r"Drone 0 and Drone 1"):
        build_start_bridge(show, hold)


def test_the_refusal_names_the_runtime_limit_it_enforces():
    hold = {0: (0.0, 0.0, -3.0), 1: (0.0, 6.0, -3.0)}
    show = {
        0: [{"time": 0.0, "x": 0.0, "y": 6.0, "z": -3.0, "yaw": 0.0}],
        1: [{"time": 0.0, "x": 0.0, "y": 0.0, "z": -3.0, "yaw": 0.0}],
    }

    with pytest.raises(StartBridgeError, match=str(DEFAULT_LIMITS.min_separation_m)):
        build_start_bridge(show, hold)


def test_legs_that_pass_over_each_other_at_different_heights_are_fine():
    """Same crossing in plan view, but 4 m apart vertically."""
    hold = {0: (0.0, 0.0, -2.0), 1: (0.0, 6.0, -6.0)}
    show = {
        0: [{"time": 0.0, "x": 0.0, "y": 6.0, "z": -2.0, "yaw": 0.0}],
        1: [{"time": 0.0, "x": 0.0, "y": 0.0, "z": -6.0, "yaw": 0.0}],
    }

    plan, bridge_s = build_start_bridge(show, hold)

    assert bridge_s > 0.0


# --- the leg scales with the fleet and with the vehicle ------------------------


def _fleet_at_hold(n: int) -> tuple[dict, dict]:
    """The real layout: pad grid, altitude layers (up to the 120 m ceiling), a
    show that starts on every pad."""
    grid, alts = launch_grid(n), altitude_layers(n)
    hold = {i: (grid[i][0], grid[i][1], -alts[i]) for i in range(n)}
    show = {
        i: [
            {"time": 0.0, "x": grid[i][0], "y": grid[i][1], "z": 0.0, "yaw": 0.0},
            {"time": 10.0, "x": grid[i][0], "y": grid[i][1], "z": -3.0, "yaw": 0.0},
        ]
        for i in range(n)
    }
    return show, hold


def test_the_peak_speed_is_a_parameter_and_is_respected():
    show, hold = _show_from_pad(3), _hold(3)

    slow_plan, slow_s = build_start_bridge(show, hold, peak_speed_ms=1.0)
    fast_plan, fast_s = build_start_bridge(show, hold, peak_speed_ms=4.0)

    assert slow_s > fast_s
    dt = 0.02
    for plan, peak in ((slow_plan, 1.0), (fast_plan, 4.0)):
        fastest = max(
            math.dist(_sample(plan, i, k * dt), _sample(plan, i, (k - 1) * dt)) / dt
            for i in range(3)
            for k in range(1, 400)
        )
        assert fastest <= peak * 1.05


def test_on_the_sim_backend_the_leg_is_flown_at_the_vehicles_own_speed_limit():
    """The tallest layer is 120 m at 100+ drones. At PX4's 1.4 m/s descent cap that
    leg is 160 s, against ~19 s for the plain dive it replaces (measured on the
    sim backend at 500 drones, issue #172): a start that is 8x later for no
    reason, since the sim vehicle is limited by max_velocity_ms, not by PX4."""
    show, hold = _fleet_at_hold(100)
    sim = DroneManager(ports=[14540], backend="sim")

    _, bridge_s = build_start_bridge(show, hold, peak_speed_ms=sim.start_bridge_peak_speed_ms)

    assert sim.start_bridge_peak_speed_ms > BRIDGE_PEAK_SPEED_MS
    assert sim.start_bridge_peak_speed_ms <= DEFAULT_LIMITS.max_velocity_ms
    assert bridge_s <= 50.0


def test_on_px4_the_leg_stays_under_the_autopilots_descent_cap():
    px4 = DroneManager(ports=[14540], backend="px4")

    assert px4.start_bridge_peak_speed_ms == BRIDGE_PEAK_SPEED_MS
    assert BRIDGE_PEAK_SPEED_MS < 1.5  # PX4 MPC_Z_VEL_MAX_DN default


def test_a_full_size_fleet_leg_is_still_swept_for_separation():
    """500 drones, the Dandiya size: the sweep must run, and must pass because
    drones stay over their own pads (2.2 m apart on the grid)."""
    show, hold = _fleet_at_hold(500)

    plan, bridge_s = build_start_bridge(show, hold, peak_speed_ms=DEFAULT_LIMITS.max_velocity_ms * 0.75)

    assert bridge_s > 0.0
    assert len(plan) == 500


# --- the separation sweep is exact between samples (review blocker B-1) --------


def _head_on(true_min_m: float, half_span_m: float) -> tuple[dict, dict]:
    """Two drones that swap places along x, offset by ``true_min_m`` in y: their
    legs have the same shape, so the closest approach is exactly ``true_min_m``
    at the middle of the leg."""
    z = -3.0

    def wp(x, y):
        return [{"time": 0.0, "x": x, "y": y, "z": z, "yaw": 0.0}]

    hold = {0: (-half_span_m, 0.0, z), 1: (half_span_m, true_min_m, z)}
    show = {0: wp(half_span_m, 0.0), 1: wp(-half_span_m, true_min_m)}
    return show, hold


SIM_SPEED_MS = 4.8  # DroneManager.start_bridge_peak_speed_ms on the sim backend
#: Half-spans chosen so the leg has an odd and an even number of 0.1 s steps: a
#: pair at the middle of the leg is on a sample for one and between two for the
#: other, which is what a 0.1 s sampled sweep gets wrong half the time.
HALF_SPANS = [3.0 + 0.1 * k for k in range(25)]


@pytest.mark.parametrize("half_span", HALF_SPANS)
def test_a_head_on_pass_between_two_samples_is_refused_at_sim_speed(half_span):
    """True closest approach 1.45 m < 1.5 m. At 4.8 m/s a head-on pair closes
    0.96 m per 0.1 s sample, so a sweep that only looks at the samples can step
    over the minimum: the old sweep accepted about half of these."""
    show, hold = _head_on(1.45, half_span)

    with pytest.raises(StartBridgeError, match=r"Drone 0 and Drone 1"):
        build_start_bridge(show, hold, peak_speed_ms=SIM_SPEED_MS)


@pytest.mark.parametrize("half_span", HALF_SPANS)
def test_a_head_on_pass_that_really_clears_the_limit_is_still_accepted(half_span):
    show, hold = _head_on(1.6, half_span)

    plan, bridge_s = build_start_bridge(show, hold, peak_speed_ms=SIM_SPEED_MS)

    assert bridge_s > 0.0


def test_the_reported_separation_is_the_true_closest_approach_not_a_sample():
    show, hold = _head_on(1.45, 3.7)

    with pytest.raises(StartBridgeError, match=r"separated by 1\.45m"):
        build_start_bridge(show, hold, peak_speed_ms=SIM_SPEED_MS)


def test_a_pass_at_exactly_the_limit_is_compliant():
    show, hold = _head_on(DEFAULT_LIMITS.min_separation_m, 3.7)

    build_start_bridge(show, hold, peak_speed_ms=SIM_SPEED_MS)


def test_a_first_waypoint_after_t0_is_held_not_drifted_to():
    """A show whose first waypoint is at t > 0 is clamped to that point until
    then. The leg must end *on* it at ``bridge_s`` and hold, not start easing
    from the leg's last sample towards the shifted waypoint."""
    show = {0: [
        {"time": 2.0, "x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0},
        {"time": 5.0, "x": 0.0, "y": 0.0, "z": -3.0, "yaw": 0.0},
    ]}

    plan, bridge_s = build_start_bridge(show, {0: (0.0, 0.0, -4.0)})

    assert _sample(plan, 0, bridge_s) == pytest.approx((0.0, 0.0, 0.0))
    assert _sample(plan, 0, bridge_s + 1.0) == pytest.approx((0.0, 0.0, 0.0))
    assert _sample(plan, 0, bridge_s + 5.0) == pytest.approx((0.0, 0.0, -3.0))
