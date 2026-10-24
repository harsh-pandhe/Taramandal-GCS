"""Waypoint interpolation and coordinate helpers.

Both the Studio validator and the GCS uploader previously carried their own
copy of this interpolation — with subtly different behaviour, since only the
Studio copy wrapped yaw correctly across the 0/360 boundary. This is the single
implementation.
"""

from __future__ import annotations

import math
from bisect import bisect_left

#: WGS-84 mean Earth radius, metres. Matches the constant the exporter and the
#: GCS's GLOBAL playback mode already used.
EARTH_RADIUS_M = 6378137.0


def _waypoint_time(w: dict) -> float:
    return w["time"]


def interpolate_waypoint(waypoints: list[dict], t: float) -> dict | None:
    """Return the interpolated state of one drone at time ``t``.

    Positions are interpolated linearly. Yaw is interpolated along the shorter
    arc, so a drone going from 350 deg to 10 deg turns 20 deg forward rather
    than 340 deg backward.

    Within a segment, ``vx``/``vy``/``vz`` (m/s) are also included — the
    segment's constant velocity, i.e. the analytic derivative of the linear
    position interpolation above — so callers driving a velocity-feedforward
    setpoint (the trajectory playback loop) don't need a second copy of this
    function. Callers that only need position (the safety validator) simply
    don't read those keys.

    Args:
        waypoints: Time-ordered waypoints for a single drone.
        t: Time in seconds.

    Returns:
        A dict with ``time``/``x``/``y``/``z``/``yaw`` (plus ``vx``/``vy``/``vz``
        mid-segment), or ``None`` if the waypoint list is empty. Outside the
        trajectory's time span the first or last waypoint is returned
        unchanged (clamped, not extrapolated) — with no velocity keys, since a
        held endpoint isn't moving; callers should treat a missing key as 0.
    """
    if not waypoints:
        return None
    if t <= waypoints[0]["time"]:
        return waypoints[0]
    if t >= waypoints[-1]["time"]:
        return waypoints[-1]

    # Binary search for the first waypoint at or after ``t``: the segment is
    # (i-1, i). It is the same segment the former linear scan picked (the first
    # with w1.time <= t <= w2.time), including at duplicate timestamps, but costs
    # O(log n) per call instead of O(n); validation samples every drone at every
    # step (issue #156).
    i = bisect_left(waypoints, t, key=_waypoint_time)
    w1, w2 = waypoints[i - 1], waypoints[i]
    span = w2["time"] - w1["time"]
    ratio = (t - w1["time"]) / span if span > 0 else 0.0

    yaw1 = w1.get("yaw", 0.0)
    yaw2 = w2.get("yaw", 0.0)
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


def quintic_profile(ratio: float) -> tuple[float, float, float, float]:
    """Minimum-jerk scalar ramp from 0 to 1, and its first three derivatives.

    ``6r^5 - 15r^4 + 10r^3`` is the unique quintic with zero velocity *and*
    zero acceleration at both ``r=0`` and ``r=1`` — smoothstep
    (``3r^2 - 2r^3``, what ``tara_studio.generator._ease`` used) only zeros
    velocity there, so acceleration still jumps discontinuously the instant a
    phase starts or ends (a delta-function jerk spike). This is the standard
    minimum-jerk polynomial (Flash & Hogan 1985) and is what makes every
    transition segment's acceleration continuous with the hold phase on
    either side of it.

    Args:
        ratio: Progress through the ramp. Clamped to ``[0, 1]``.

    Returns:
        ``(position, velocity, acceleration, jerk)`` — the derivatives are
        with respect to ``ratio``, i.e. normalized to a ramp of duration 1.
        Callers driving a ramp of duration ``T`` seconds must scale
        ``velocity`` by ``1/T``, ``acceleration`` by ``1/T**2`` and ``jerk``
        by ``1/T**3`` (see :func:`quintic_segment`).
    """
    r = min(1.0, max(0.0, ratio))
    position = 6 * r**5 - 15 * r**4 + 10 * r**3
    velocity = 30 * r**4 - 60 * r**3 + 30 * r**2
    acceleration = 120 * r**3 - 180 * r**2 + 60 * r
    jerk = 360 * r**2 - 360 * r + 60
    return position, velocity, acceleration, jerk


def quintic_segment(
    p0: tuple[float, float, float],
    p1: tuple[float, float, float],
    duration: float,
    t: float,
) -> dict:
    """State of a minimum-jerk 3D transition from ``p0`` to ``p1``.

    Both endpoints are at rest (zero velocity, zero acceleration) — matching
    how a generator phase currently starts and ends — so this is a drop-in
    replacement for a linearly- or smoothstep-interpolated transition segment
    wherever continuous acceleration matters more than a shaped intermediate
    path.

    Args:
        p0: Position at ``t=0``.
        p1: Position at ``t=duration``.
        duration: Segment length, seconds. ``t`` is clamped into
            ``[0, duration]``, and ``duration <= 0`` collapses to ``p1`` at
            rest (an instantaneous jump has no well-defined rate).
        t: Time into the segment, seconds.

    Returns:
        A dict with ``position``, ``velocity``, ``acceleration`` and
        ``jerk``, each an ``(x, y, z)`` tuple (velocity in m/s, etc).
    """
    if duration <= 0:
        return {
            "position": p1,
            "velocity": (0.0, 0.0, 0.0),
            "acceleration": (0.0, 0.0, 0.0),
            "jerk": (0.0, 0.0, 0.0),
        }

    ratio = t / duration
    s, s_dot, s_ddot, s_dddot = quintic_profile(ratio)
    delta = tuple(b - a for a, b in zip(p0, p1))

    return {
        "position": tuple(a + d * s for a, d in zip(p0, delta)),
        "velocity": tuple(d * s_dot / duration for d in delta),
        "acceleration": tuple(d * s_ddot / duration**2 for d in delta),
        "jerk": tuple(d * s_dddot / duration**3 for d in delta),
    }


def jerk_from_positions(
    p_m2: tuple[float, float, float],
    p_m1: tuple[float, float, float],
    p_p1: tuple[float, float, float],
    p_p2: tuple[float, float, float],
    dt: float,
) -> float:
    """Jerk magnitude at the midpoint of five evenly-spaced samples.

    Uses the standard symmetric central-difference stencil for a third
    derivative, ``f''' = (-f(-2h) + 2f(-h) - 2f(h) + f(2h)) / (2h^3)`` —
    the same finite-difference family ``tara_studio.validator._curvature``
    uses for centripetal acceleration (a central second difference), one
    order further along the derivative chain. The midpoint sample itself
    isn't needed, matching that a 3rd-derivative central stencil is odd
    (antisymmetric) rather than centred on ``f(0)``.

    Args:
        p_m2: Position two samples before the midpoint.
        p_m1: Position one sample before the midpoint.
        p_p1: Position one sample after the midpoint.
        p_p2: Position two samples after the midpoint.
        dt: Sample interval, seconds. Must be positive.

    Returns:
        The 3D jerk magnitude, m/s^3.
    """
    jx = (-p_m2[0] + 2 * p_m1[0] - 2 * p_p1[0] + p_p2[0]) / (2 * dt**3)
    jy = (-p_m2[1] + 2 * p_m1[1] - 2 * p_p1[1] + p_p2[1]) / (2 * dt**3)
    jz = (-p_m2[2] + 2 * p_m1[2] - 2 * p_p1[2] + p_p2[2]) / (2 * dt**3)
    return (jx * jx + jy * jy + jz * jz) ** 0.5


def sweep_times(duration: float, dt: float = 0.1) -> list[float]:
    """Return sample times covering ``[0, duration]`` inclusive.

    Uses ``ceil`` so the sweep always reaches the end of the timeline. With
    ``round`` a trajectory whose duration is not an exact multiple of ``dt``
    (5.44 s, say) would stop sampling early and leave the final segment
    unchecked — which is precisely where a landing convergence tends to put the
    closest approach.
    """
    if duration <= 0:
        return [0.0]
    steps = math.ceil(duration / dt)
    return [step * dt for step in range(steps + 1)]


def ned_to_geodetic(
    x: float, y: float, home_lat: float, home_lon: float
) -> tuple[float, float]:
    """Convert local NED metres to latitude/longitude degrees.

    Uses an equirectangular approximation about ``home``, which is accurate to
    well under a centimetre over the few hundred metres a drone show spans.

    Args:
        x: Metres north of home.
        y: Metres east of home.
        home_lat: Home latitude, degrees.
        home_lon: Home longitude, degrees.

    Returns:
        ``(latitude, longitude)`` in degrees.
    """
    lat = home_lat + math.degrees(x / EARTH_RADIUS_M)
    lon = home_lon + math.degrees(
        y / (EARTH_RADIUS_M * math.cos(math.radians(home_lat)))
    )
    return lat, lon


def geodetic_to_ned(
    lat: float, lon: float, home_lat: float, home_lon: float
) -> tuple[float, float]:
    """Convert latitude/longitude degrees to local NED metres about ``home``.

    The exact inverse of :func:`ned_to_geodetic` under the same equirectangular
    approximation, so a value round-tripped through both functions returns
    unchanged. Used when a GLOBAL-mode setpoint (lat/lon) needs to drive a
    controller that operates in local NED, e.g. the simulator's fleet physics.

    Args:
        lat: Target latitude, degrees.
        lon: Target longitude, degrees.
        home_lat: Home latitude, degrees.
        home_lon: Home longitude, degrees.

    Returns:
        ``(x, y)`` metres (north, east) of ``home``.
    """
    x = math.radians(lat - home_lat) * EARTH_RADIUS_M
    y = math.radians(lon - home_lon) * EARTH_RADIUS_M * math.cos(math.radians(home_lat))
    return x, y
