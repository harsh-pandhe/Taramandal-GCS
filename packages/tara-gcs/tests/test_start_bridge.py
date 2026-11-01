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

from tara_core import DEFAULT_LIMITS
from tara_core.geometry import interpolate_waypoint
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


def test_drones_with_no_position_are_carried_along_not_dropped():
    show = _show_from_pad(2)

    plan, bridge_s = build_start_bridge(show, {0: _hold(2)[0]})

    assert set(plan) == {0, 1}
    assert _sample(plan, 1, 0.0) == pytest.approx((0.0, 2.2, 0.0))  # clamped to its first waypoint
    assert plan[1][-1]["time"] == pytest.approx(show[1][-1]["time"] + bridge_s)


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
