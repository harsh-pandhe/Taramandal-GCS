"""LED colour of one drone at a point in its trajectory.

Studio bakes each drone's light colour into its waypoints (``r``, ``g``, ``b``,
and optionally ``w``, 0-255), so a show's colours, fades, flashes and
dark-flight stretches all travel inside the trajectory file. The GCS publishes
the colour a drone is *commanded* to show at the current playback time next to
its planned position, so a dashboard can paint the sky the way Studio does.
"""

from __future__ import annotations

from bisect import bisect_left


def _time(waypoint: dict) -> float:
    return waypoint["time"]


def led_at(waypoints: list[dict], t: float) -> tuple[float, float, float] | None:
    """``(r, g, b)`` in 0-255 at time ``t``, or ``None`` when the drone carries no colour data.

    Colour is blended linearly between the two waypoints either side of ``t``,
    the same way position is, and held at the first/last waypoint outside the
    trajectory's time span. A drone is treated as having colour data only when
    its first waypoint names ``r``, ``g`` and ``b``; a waypoint that omits a
    channel mid-show counts as 0 (dark) for that channel.
    """
    if not waypoints:
        return None
    first = waypoints[0]
    if "r" not in first or "g" not in first or "b" not in first:
        return None
    if t <= first["time"]:
        w = first
        return (float(w.get("r", 0)), float(w.get("g", 0)), float(w.get("b", 0)))
    last = waypoints[-1]
    if t >= last["time"]:
        return (float(last.get("r", 0)), float(last.get("g", 0)), float(last.get("b", 0)))
    i = bisect_left(waypoints, t, key=_time)
    w1, w2 = waypoints[i - 1], waypoints[i]
    span = w2["time"] - w1["time"]
    ratio = (t - w1["time"]) / span if span > 0 else 0.0
    return tuple(
        float(w1.get(c, 0)) + (float(w2.get(c, 0)) - float(w1.get(c, 0))) * ratio for c in ("r", "g", "b")
    )
