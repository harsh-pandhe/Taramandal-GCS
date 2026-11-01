"""The transition leg from the formation hold to a streamed show's first waypoint.

The launch gate parks every drone at its altitude layer (2, 4, 6 ... m,
``tara_core.layout.altitude_layers``) and only then allows a start. A Studio show
begins on the pad (z = 0) and carries its own takeoff leg, so streaming it from
the first tick commanded every drone to jump to its pad: a position step of the
whole hold altitude, followed by whatever PX4's position controller made of it
(issue #172, F3 - drone 0 went 1.9 m -> 0.7 m in about 1.3 s on SITL).

:func:`build_start_bridge` replaces that step with a short minimum-jerk leg from
where each drone actually is to the show's first waypoint, then plays the show
unchanged, delayed by the leg. The leg is swept with the same collision check
the upload gate uses and the start is refused if it fails, so the hold gate is
still the only way in and a start can only get safer, never less checked.
"""

from __future__ import annotations

import math

from tara_core import DEFAULT_LIMITS
from tara_core.validate import collision_errors

#: A drone this close to its show's first waypoint (metres) needs no leg. Same
#: figure as ``DroneManager.verify_launch_geometry``'s default tolerance, so a
#: show the geometry check accepts as "placed correctly" starts as it always did.
BRIDGE_TOLERANCE_M = 0.5
#: Peak speed of the leg (m/s), well inside ``DEFAULT_LIMITS.max_velocity_ms``
#: and under PX4's default descent cap (``MPC_Z_VEL_MAX_DN``, 1.5 m/s): the leg
#: is mostly a descent to the pad, and a plan faster than the vehicle may fly
#: just shows up as tracking lag (0.8 m at 2 m/s on SITL, issue #172). The
#: longest leg is the highest altitude layer back to the pad, so this sets the
#: delay before a show's t = 0 (about 8 s for a 6 m descent).
BRIDGE_PEAK_SPEED_MS = 1.4
#: Shortest leg, so a small correction is still eased in rather than stepped.
MIN_BRIDGE_S = 1.0
#: Spacing of the leg's waypoints. Matches the 10 Hz setpoint loop, which
#: interpolates linearly between them.
BRIDGE_SAMPLE_S = 0.1
#: A minimum-jerk profile peaks at 1.875x its average speed.
_MIN_JERK_PEAK_FACTOR = 1.875


class StartBridgeError(ValueError):
    """The transition to the show's first waypoints is not safe to fly."""


def _min_jerk(tau: float) -> float:
    """Position fraction at normalised time ``tau`` (0..1); zero velocity and
    acceleration at both ends."""
    return tau**3 * (10.0 - 15.0 * tau + 6.0 * tau * tau)


def build_start_bridge(
    plan: dict[int, list[dict]],
    hold: dict[int, tuple[float, float, float]],
) -> tuple[dict[int, list[dict]], float]:
    """Prepend a transition leg to ``plan`` and shift the show by its duration.

    Args:
        plan: The show about to be streamed, ``{drone_id: [waypoint, ...]}`` in
            the fleet frame (x north, y east, z down).
        hold: Where each drone is now, ``{drone_id: (x, y, z)}`` in the same
            frame. A drone with no entry (not connected) is carried along
            unchanged: its first waypoint is simply held for the leg.

    Returns:
        ``(plan, bridge_s)``. When every drone is within
        :data:`BRIDGE_TOLERANCE_M` of its first waypoint this is ``(plan, 0.0)``
        - the same object, so a show that starts at the hold behaves exactly as
        before. Otherwise a new plan, in which every drone's show is delayed by
        the same ``bridge_s`` and preceded by its leg. ``plan`` is not modified.

    Raises:
        StartBridgeError: If the legs bring two drones closer than the runtime
            separation limit (``SafetyLimits.min_separation_m``) - the distance
            the live proximity monitor aborts on.
    """
    legs: dict[int, tuple[tuple[float, float, float], tuple[float, float, float], float]] = {}
    for drone_id, waypoints in plan.items():
        if not waypoints or drone_id not in hold:
            continue
        first = waypoints[0]
        goal = (first["x"], first["y"], first["z"])
        legs[drone_id] = (hold[drone_id], goal, first.get("yaw", 0.0))

    longest = max((math.dist(start, goal) for start, goal, _ in legs.values()), default=0.0)
    if longest <= BRIDGE_TOLERANCE_M:
        return plan, 0.0

    steps = max(
        math.ceil(MIN_BRIDGE_S / BRIDGE_SAMPLE_S),
        math.ceil(_MIN_JERK_PEAK_FACTOR * longest / BRIDGE_PEAK_SPEED_MS / BRIDGE_SAMPLE_S),
    )
    bridge_s = steps * BRIDGE_SAMPLE_S

    bridged: dict[int, list[dict]] = {}
    for drone_id, waypoints in plan.items():
        shifted = [{**w, "time": w["time"] + bridge_s} for w in waypoints]
        leg = legs.get(drone_id)
        if leg is None:
            bridged[drone_id] = shifted
            continue
        start, goal, yaw = leg
        # The last step is the show's own first waypoint (now at bridge_s), so
        # the leg stops one sample short rather than duplicating that instant.
        samples = []
        for k in range(steps):
            f = _min_jerk(k / steps)
            x, y, z = (a + f * (b - a) for a, b in zip(start, goal))
            samples.append({"time": k * BRIDGE_SAMPLE_S, "x": x, "y": y, "z": z, "yaw": yaw})
        bridged[drone_id] = samples + shifted

    # The show was checked when it was uploaded; only the leg is new. Sweep it
    # through the instant the show takes over, at the limit the fleet aborts on.
    transition = {
        drone_id: [w for w in waypoints if w["time"] <= bridge_s] for drone_id, waypoints in bridged.items()
    }
    errors = collision_errors(transition, limits=DEFAULT_LIMITS)
    if errors:
        raise StartBridgeError(
            f"Start refused: the transition from the formation hold to the show's first waypoints is not safe "
            f"(runtime separation limit {DEFAULT_LIMITS.min_separation_m}m). " + "; ".join(errors)
        )
    return bridged, bridge_s
