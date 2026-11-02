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

import numpy as np
from scipy.spatial import cKDTree

from tara_core import DEFAULT_LIMITS

#: A drone this close to its show's first waypoint (metres) needs no leg. Same
#: figure as ``DroneManager.verify_launch_geometry``'s default tolerance, so a
#: show the geometry check accepts as "placed correctly" starts as it always did.
BRIDGE_TOLERANCE_M = 0.5
#: Peak speed of the leg (m/s) for a real PX4 vehicle, under its default descent
#: cap (``MPC_Z_VEL_MAX_DN``, 1.5 m/s): the leg is mostly a descent to the pad,
#: and a plan faster than the vehicle may fly just shows up as tracking lag
#: (0.8 m at 2 m/s on SITL, issue #172). The longest leg is the highest
#: altitude layer back to the pad, so the speed sets the delay before a show's
#: t = 0 (about 8 s for a 6 m descent). The sim backend has no such cap; the
#: caller passes its own speed (``DroneManager.start_bridge_peak_speed_ms``).
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
    peak_speed_ms: float = BRIDGE_PEAK_SPEED_MS,
) -> tuple[dict[int, list[dict]], float]:
    """Prepend a transition leg to ``plan`` and shift the show by its duration.

    Args:
        plan: The show about to be streamed, ``{drone_id: [waypoint, ...]}`` in
            the fleet frame (x north, y east, z down).
        hold: Where each drone is now, ``{drone_id: (x, y, z)}`` in the same
            frame. Every drone in ``plan`` that has waypoints needs an entry:
            one without a known position cannot be given a leg, and streaming
            it anyway would step it to its first waypoint - the dive this
            module exists to remove.
        peak_speed_ms: Peak speed of the leg. The longest leg sets the duration,
            ``1.875 * distance / peak_speed_ms``, the same for every drone.

    Returns:
        ``(plan, bridge_s)``. When every drone is within
        :data:`BRIDGE_TOLERANCE_M` of its first waypoint this is ``(plan, 0.0)``
        - the same object, so a show that starts at the hold behaves exactly as
        before. Otherwise a new plan, in which every drone's show is delayed by
        the same ``bridge_s`` and preceded by its leg. ``plan`` is not modified.

    Raises:
        StartBridgeError: If a drone in ``plan`` has no entry in ``hold`` (and
            a leg is needed, or no drone's position is known at all, so it
            cannot be shown that none is). Or if the legs bring two drones
            closer than the runtime separation limit
            (``SafetyLimits.min_separation_m``) - the distance the live
            proximity monitor aborts on - at any instant, not just at the
            samples (see :func:`_leg_separation_errors`).
    """
    legs: dict[int, tuple[tuple[float, float, float], tuple[float, float, float], float]] = {}
    unknown: list[int] = []
    for drone_id, waypoints in plan.items():
        if not waypoints:
            continue
        if drone_id not in hold:
            unknown.append(drone_id)
            continue
        first = waypoints[0]
        goal = (first["x"], first["y"], first["z"])
        legs[drone_id] = (hold[drone_id], goal, first.get("yaw", 0.0))

    longest = max((math.dist(start, goal) for start, goal, _ in legs.values()), default=0.0)
    if unknown and (longest > BRIDGE_TOLERANCE_M or not legs):
        raise StartBridgeError(
            "Start refused: no current position for "
            + ", ".join(f"Drone {d}" for d in sorted(unknown))
            + " (not connected, no fresh telemetry, or never reported), so its transition from the "
            "formation hold to the show's first waypoint cannot be planned or checked."
        )
    if longest <= BRIDGE_TOLERANCE_M:
        return plan, 0.0

    steps = max(
        math.ceil(MIN_BRIDGE_S / BRIDGE_SAMPLE_S),
        math.ceil(_MIN_JERK_PEAK_FACTOR * longest / peak_speed_ms / BRIDGE_SAMPLE_S),
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
        if shifted[0]["time"] > bridge_s:
            # The show is clamped to its first waypoint until that waypoint's
            # own time. Say so at the instant the leg ends, or the leg's last
            # sample would drift towards it instead.
            shifted.insert(0, {**shifted[0], "time": bridge_s})
        # The last step is the show's own first waypoint (now at bridge_s), so
        # the leg stops one sample short rather than duplicating that instant.
        samples = []
        for k in range(steps):
            f = _min_jerk(k / steps)
            x, y, z = (a + f * (b - a) for a, b in zip(start, goal))
            samples.append({"time": k * BRIDGE_SAMPLE_S, "x": x, "y": y, "z": z, "yaw": yaw})
        bridged[drone_id] = samples + shifted

    # The show was checked when it was uploaded; only the leg is new.
    errors = _leg_separation_errors(legs, steps)
    if errors:
        raise StartBridgeError(
            f"Start refused: the transition from the formation hold to the show's first waypoints is not safe "
            f"(runtime separation limit {DEFAULT_LIMITS.min_separation_m}m). " + "; ".join(errors)
        )
    return bridged, bridge_s


def _leg_separation_errors(
    legs: dict[int, tuple[tuple[float, float, float], tuple[float, float, float], float]],
    steps: int,
    max_errors: int = 3,
) -> list[str]:
    """Pairs of drones that come closer than the runtime limit during the legs.

    Exact, not sampled. Every leg has its waypoints at the same instants
    (``k * BRIDGE_SAMPLE_S``) and the setpoint loop interpolates linearly
    between them, so within one interval the vector between any two drones is
    ``r0 + dv * u`` for ``u`` in ``[0, 1]`` and its closest approach is
    closed-form. Checking only the instants (what ``collision_errors`` does at
    0.1 s) can step over the minimum: at the sim backend's 4.8 m/s a head-on
    pair closes 0.96 m per sample, so a true 1.45 m pass can be seen as 1.9 m
    (issue #172 F3, review). Candidate pairs per interval come from a KD-tree
    at the limit plus the most the pair can close in that interval.

    Returns:
        Error strings in the same form as :func:`tara_core.validate.collision_errors`,
        at most ``max_errors``.
    """
    ids = list(legs)
    if len(ids) < 2:
        return []
    limit = DEFAULT_LIMITS.min_separation_m
    start = np.array([legs[i][0] for i in ids], dtype=float)
    goal = np.array([legs[i][1] for i in ids], dtype=float)
    fractions = np.array([_min_jerk(k / steps) for k in range(steps + 1)])
    points = start[None, :, :] + fractions[:, None, None] * (goal - start)[None, :, :]

    errors: list[str] = []
    for k in range(steps):
        a, delta = points[k], points[k + 1] - points[k]
        reach = 2.0 * float(np.linalg.norm(delta, axis=1).max())
        pairs = cKDTree(a).query_pairs(limit + reach, output_type="ndarray")
        if not len(pairs):
            continue
        i, j = pairs[:, 0], pairs[:, 1]
        r0, dv = a[j] - a[i], delta[j] - delta[i]
        speed2 = np.einsum("ij,ij->i", dv, dv)
        u = np.where(speed2 > 0.0, -np.einsum("ij,ij->i", r0, dv) / np.where(speed2 > 0.0, speed2, 1.0), 0.0)
        u = np.clip(u, 0.0, 1.0)
        dist = np.linalg.norm(r0 + dv * u[:, None], axis=1)
        for n in np.argsort(dist):
            if dist[n] >= limit:
                break
            a_id, b_id = sorted((ids[int(i[n])], ids[int(j[n])]))
            errors.append(
                f"Collision risk at {(k + u[n]) * BRIDGE_SAMPLE_S:.1f}s: Drone {a_id} and Drone {b_id} are "
                f"separated by {dist[n]:.2f}m (limit: {limit}m)"
            )
            if len(errors) >= max_errors:
                return errors
    return errors
