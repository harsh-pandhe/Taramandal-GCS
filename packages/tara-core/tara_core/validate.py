"""Whole-show safety validation.

Two entry points, deliberately enforcing *different* thresholds:

  * :func:`validate_show` is the design-time check the Studio runs. It uses
    ``design_separation_m`` (the stricter number) so a show is certified with
    margin to spare.
  * :func:`collision_errors` is the gate the GCS applies at upload, immediately
    before a show can fly. It enforces ``min_separation_m`` — the hard floor the
    live proximity monitor aborts on.

Design stricter than runtime is the whole point: a show that only just squeaks
past the runtime limit should never have been certified in the first place.
"""

from __future__ import annotations

from .geometry import interpolate_waypoint, sweep_times
from .proximity import close_pairs, min_separation
from .safety import DEFAULT_LIMITS, SafetyLimits
from .show import Show

#: Violations retained per category, to keep API responses a sane size.
_MAX_REPORTED = 50
#: Of those, how many are the most severe; the rest sample the timeline evenly.
_MOST_SEVERE = _MAX_REPORTED // 2


def _report_subset(found: list[tuple[float, dict]]) -> list[dict]:
    """Pick at most ``_MAX_REPORTED`` violations of one kind to return, in time order.

    ``found`` is every violation of that kind in time order, each paired with
    how far past its limit it went. Keeping only the earliest ones hid a show's
    worst moment whenever it came late (issue #138), so the most severe half is
    always kept, the global worst among them, and the other half samples the
    rest of the timeline evenly.
    """
    if len(found) <= _MAX_REPORTED:
        return [entry for _, entry in found]
    # sorted() is stable, so equally severe violations keep their time order.
    by_severity = sorted(range(len(found)), key=lambda i: found[i][0], reverse=True)
    severe = set(by_severity[:_MOST_SEVERE])
    rest = [i for i in range(len(found)) if i not in severe]
    step = max(1, len(rest) // (_MAX_REPORTED - _MOST_SEVERE))
    sample = rest[::step][: _MAX_REPORTED - _MOST_SEVERE]
    return [found[i][1] for i in sorted(severe.union(sample))]


def _positions_at(show: Show, t: float) -> tuple[dict, dict]:
    """Return ``(xyz_by_id, full_state_by_id)`` for every drone at time ``t``."""
    xyz, states = {}, {}
    for drone_id, wps in show.drones.items():
        wp = interpolate_waypoint(wps, t)
        if wp is not None:
            xyz[drone_id] = (wp["x"], wp["y"], wp["z"])
            states[drone_id] = wp
    return xyz, states


def validate_show(
    data,
    limits: SafetyLimits = DEFAULT_LIMITS,
    separation: float | None = None,
    dt: float = 0.1,
) -> dict:
    """Sweep a show's timeline and report every safety violation.

    Args:
        data: A :class:`~tara_core.show.Show` or either supported dict shape.
        limits: Safety limits to enforce.
        separation: Override the proximity threshold. Defaults to
            ``limits.design_separation_m``.
        dt: Sweep interval in seconds.

    Returns:
        A report dict with ``status`` (``"PASSED"``/``"FAILED"``), ``summary``,
        ``metrics``, ``violations`` (at most ``_MAX_REPORTED`` per kind, see
        :func:`_report_subset`) and ``worst_violations`` (the single worst of
        each kind, or ``None``). Each violation carries the limit it broke.
    """
    show = Show.from_any(data)
    threshold = limits.design_separation_m if separation is None else separation

    violations: dict[str, list] = {
        "proximity": [],
        "velocity": [],
        "acceleration": [],
        "yaw_rate": [],
    }

    if not show.drones:
        return {
            "status": "FAILED",
            "summary": "No drones found in choreography.",
            "metrics": {
                "min_distance_m": 0.0,
                "max_velocity_m_s": 0.0,
                "max_acceleration_m_s2": 0.0,
                "max_yaw_rate_deg_s": 0.0,
            },
            "violations": violations,
            "worst_violations": {key: None for key in violations},
        }

    # Every violation, in time order, paired with how far past its limit it
    # went: unrounded and in its own check's units, so ranking them is exact.
    found: dict[str, list[tuple[float, dict]]] = {key: [] for key in violations}
    min_dist = float("inf")
    max_vel = max_acc = max_yaw_rate = 0.0
    prev: dict[int, dict] = {}

    for t in sweep_times(show.duration, dt):
        xyz, states = _positions_at(show, t)

        for a, b, dist in close_pairs(xyz, threshold):
            found["proximity"].append((threshold - dist, {
                "time": round(t, 2),
                "drones": [a, b],
                "distance": round(dist, 2),
                "limit": round(threshold, 2),
                "positions": {
                    str(a): [round(c, 2) for c in xyz[a]],
                    str(b): [round(c, 2) for c in xyz[b]],
                },
            }))
        min_dist = min(min_dist, min_separation(xyz))

        for drone_id, wp in states.items():
            last = prev.get(drone_id)
            if last is None:
                prev[drone_id] = {
                    "pos": (wp["x"], wp["y"], wp["z"]),
                    "vel": (0.0, 0.0, 0.0),
                    "yaw": wp.get("yaw", 0.0),
                }
                continue

            vx = (wp["x"] - last["pos"][0]) / dt
            vy = (wp["y"] - last["pos"][1]) / dt
            vz = (wp["z"] - last["pos"][2]) / dt
            speed = (vx * vx + vy * vy + vz * vz) ** 0.5
            max_vel = max(max_vel, speed)

            ax = (vx - last["vel"][0]) / dt
            ay = (vy - last["vel"][1]) / dt
            az = (vz - last["vel"][2]) / dt
            accel = (ax * ax + ay * ay + az * az) ** 0.5
            max_acc = max(max_acc, accel)

            dyaw = (wp.get("yaw", 0.0) - last["yaw"]) % 360
            if dyaw > 180:
                dyaw -= 360
            yaw_rate = abs(dyaw) / dt
            max_yaw_rate = max(max_yaw_rate, yaw_rate)

            if speed > limits.max_velocity_ms:
                found["velocity"].append((speed - limits.max_velocity_ms, {
                    "time": round(t, 2),
                    "drone": drone_id,
                    "velocity": round(speed, 2),
                    "limit": round(limits.max_velocity_ms, 2),
                }))
            if accel > limits.max_accel_ms2:
                found["acceleration"].append((accel - limits.max_accel_ms2, {
                    "time": round(t, 2),
                    "drone": drone_id,
                    "acceleration": round(accel, 2),
                    "limit": round(limits.max_accel_ms2, 2),
                }))
            if yaw_rate > limits.max_yaw_rate_dps:
                found["yaw_rate"].append((yaw_rate - limits.max_yaw_rate_dps, {
                    "time": round(t, 2),
                    "drone": drone_id,
                    "yaw_rate": round(yaw_rate, 2),
                    "limit": round(limits.max_yaw_rate_dps, 2),
                }))

            prev[drone_id] = {
                "pos": (wp["x"], wp["y"], wp["z"]),
                "vel": (vx, vy, vz),
                "yaw": wp.get("yaw", 0.0),
            }

    worst_violations: dict[str, dict | None] = {key: None for key in violations}
    for key, items in found.items():
        if items:
            # max() keeps the first of equal maxima: a tie goes to the earliest.
            worst_violations[key] = max(items, key=lambda item: item[0])[1]
        violations[key] = _report_subset(items)

    failed = any(violations[k] for k in violations)
    return {
        "status": "FAILED" if failed else "PASSED",
        "summary": (
            "Choreography failed safety validation checks."
            if failed
            else "Choreography passed all safety checks!"
        ),
        "metrics": {
            "min_distance_m": round(min_dist, 2) if min_dist != float("inf") else 0.0,
            "max_velocity_m_s": round(max_vel, 2),
            "max_acceleration_m_s2": round(max_acc, 2),
            "max_yaw_rate_deg_s": round(max_yaw_rate, 2),
        },
        "violations": violations,
        "worst_violations": worst_violations,
    }


def collision_errors(
    data,
    limits: SafetyLimits = DEFAULT_LIMITS,
    separation: float | None = None,
    max_errors: int = 3,
) -> list[str]:
    """Return human-readable collision errors, or an empty list if the show is safe.

    This is the GCS upload gate. It enforces ``min_separation_m`` — the hard
    limit the live fleet aborts on — rather than the stricter design threshold,
    so a show certified by :func:`validate_show` always clears it.

    Args:
        data: A :class:`~tara_core.show.Show` or either supported dict shape.
        limits: Safety limits to enforce.
        separation: Override the threshold. Defaults to ``limits.min_separation_m``.
        max_errors: Stop after this many errors.

    Returns:
        Formatted error strings, at most ``max_errors`` of them.
    """
    show = Show.from_any(data)
    threshold = limits.min_separation_m if separation is None else separation

    errors: list[str] = []
    for t in sweep_times(show.duration, 0.1):
        xyz, _ = _positions_at(show, t)
        for a, b, dist in close_pairs(xyz, threshold):
            errors.append(
                f"Collision risk at {t:.1f}s: Drone {a} and Drone {b} are "
                f"separated by {dist:.2f}m (limit: {threshold}m)"
            )
            if len(errors) >= max_errors:
                return errors
    return errors
