"""Waypoint interpolation and coordinate helpers.

Both the Studio validator and the GCS uploader previously carried their own
copy of this interpolation — with subtly different behaviour, since only the
Studio copy wrapped yaw correctly across the 0/360 boundary. This is the single
implementation.
"""

from __future__ import annotations

import math

#: WGS-84 mean Earth radius, metres. Matches the constant the exporter and the
#: GCS's GLOBAL playback mode already used.
EARTH_RADIUS_M = 6378137.0


def interpolate_waypoint(waypoints: list[dict], t: float) -> dict | None:
    """Return the interpolated state of one drone at time ``t``.

    Positions are interpolated linearly. Yaw is interpolated along the shorter
    arc, so a drone going from 350 deg to 10 deg turns 20 deg forward rather
    than 340 deg backward.

    Args:
        waypoints: Time-ordered waypoints for a single drone.
        t: Time in seconds.

    Returns:
        A dict with ``time``/``x``/``y``/``z``/``yaw``, or ``None`` if the
        waypoint list is empty. Outside the trajectory's time span the first or
        last waypoint is returned unchanged (clamped, not extrapolated).
    """
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
            }
    return waypoints[-1]


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
