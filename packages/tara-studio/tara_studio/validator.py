"""Choreography safety validation.

The implementation now lives in :mod:`tara_core.validate` so that the Studio,
the GCS uploader and the live fleet monitor all enforce one set of limits. This
module keeps the historical ``validate_choreography`` signature so existing
callers and tests continue to work.

:func:`validate_dynamics` adds a second, stricter pass on top of that shared
check — see its docstring for why it is separate rather than folded into
``SafetyLimits``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from tara_core import DEFAULT_LIMITS, SafetyLimits
from tara_core.geometry import interpolate_waypoint, jerk_from_positions, sweep_times  # re-exported
from tara_core.proximity import min_separation
from tara_core.show import Show
from tara_core.validate import validate_show

__all__ = [
    "DEFAULT_DYNAMIC_LIMITS",
    "DynamicLimits",
    "certify_feasibility",
    "certify_feasibility_report",
    "interpolate_waypoint",
    "validate_choreography",
    "validate_dynamics",
]


def validate_choreography(
    choreo_data: dict,
    safe_dist_threshold: float | None = None,
    max_velocity: float | None = None,
    max_accel: float | None = None,
    max_yaw_rate: float | None = None,
) -> dict:
    """Validate a multi-drone choreography against the shared safety limits.

    Args:
        choreo_data: A Studio-shaped or GCS-shaped choreography dict.
        safe_dist_threshold: Minimum separation in metres. Defaults to
            ``DEFAULT_LIMITS.design_separation_m`` — deliberately stricter than
            the limit the fleet aborts on, so a certified show has margin.
        max_velocity: Speed ceiling, m/s.
        max_accel: Acceleration ceiling, m/s^2.
        max_yaw_rate: Yaw rate ceiling, deg/s.

    Returns:
        A report dict with ``status``, ``summary``, ``metrics`` and ``violations``.
    """
    limits = SafetyLimits(
        min_separation_m=DEFAULT_LIMITS.min_separation_m,
        design_separation_m=max(
            safe_dist_threshold
            if safe_dist_threshold is not None
            else DEFAULT_LIMITS.design_separation_m,
            DEFAULT_LIMITS.min_separation_m,
        ),
        max_velocity_ms=max_velocity if max_velocity is not None else DEFAULT_LIMITS.max_velocity_ms,
        max_accel_ms2=max_accel if max_accel is not None else DEFAULT_LIMITS.max_accel_ms2,
        max_yaw_rate_dps=max_yaw_rate if max_yaw_rate is not None else DEFAULT_LIMITS.max_yaw_rate_dps,
    )
    # An explicit threshold below the runtime abort limit is honoured for the
    # sweep but cannot be expressed by SafetyLimits, so pass it through directly.
    separation = safe_dist_threshold if safe_dist_threshold is not None else None
    return validate_show(choreo_data, limits=limits, separation=separation)


@dataclass(frozen=True)
class DynamicLimits:
    """Dynamic-feasibility bounds for a choreographed morph.

    These are deliberately *not* folded into :class:`~tara_core.SafetyLimits`.
    That type is shared with the GCS uploader and the live proximity monitor,
    and its ``max_velocity_ms`` (6.0) / ``max_accel_ms2`` (50.0) are the
    envelope the airframe can survive — the numbers the fleet aborts on.
    The limits here are a *choreography-quality* bound: what a show should ask
    of the fleet so that tracking error stays small and the formation still
    looks like a formation. Tightening the shared limits to these values would
    silently change the GCS abort behaviour for every existing show, which is a
    different track's decision.

    ``max_centripetal_ms2`` is also a genuinely different quantity from
    ``SafetyLimits.max_accel_ms2``: total acceleration includes the along-track
    component (speeding up and slowing down), whereas the centripetal component
    is only the part that bends the path. A drone can hold 2.5 m/s^2 of
    cornering while accelerating hard in a straight line and be well inside the
    total-acceleration limit yet outside this one.

    ``max_jerk_ms3`` (issue #60) belongs here for the same reason: it is a
    choreography-quality bound, not an airframe-survival one. A real rotor
    takes 50-150 ms to spool up or down (first-order lag, time constant
    tau ~= 0.08 s per the issue), so a trajectory with unbounded jerk demands
    an instantaneous thrust change no motor can deliver — the tracker lags the
    setpoint, and that lag is exactly the tracking error this bound keeps
    small, the same way ``max_centripetal_ms2`` keeps cornering error small.
    20 m/s^3 is the issue's target ceiling.

    Attributes:
        max_velocity_ms: Linear speed ceiling, ``|p_dot| <= 4.0 m/s``.
        max_centripetal_ms2: Cornering ceiling, ``kappa * v^2 <= 2.5 m/s^2``.
        max_jerk_ms3: Jerk ceiling, ``|p_dddot| <= 20.0 m/s^3``.
        min_separation_m: Minimum spatial distance between any two drones.
    """

    max_velocity_ms: float = 4.0
    max_centripetal_ms2: float = 2.5
    max_jerk_ms3: float = 20.0
    min_separation_m: float = 1.5


#: The bounds specified for choreography dynamic feasibility.
DEFAULT_DYNAMIC_LIMITS = DynamicLimits()

#: Violations retained per category, matching tara_core.validate's cap so an
#: API response stays a sane size on a badly-broken show.
_MAX_REPORTED = 50


def _curvature(p_prev, p_curr, p_next, dt: float) -> tuple[float, float]:
    """Discrete curvature ``kappa`` and speed at ``p_curr``, from three samples.

    Uses the standard finite-difference form
    ``kappa = |v x a| / |v|^3`` with the centred velocity estimate
    ``v = (p_next - p_prev) / 2dt`` and the second difference
    ``a = (p_next - 2*p_curr + p_prev) / dt^2``.

    The cross product is what makes this the *centripetal* component rather
    than total acceleration: ``|v x a| / |v|`` is exactly the magnitude of the
    acceleration perpendicular to the direction of travel, so the along-track
    part cancels out and never inflates the result.

    Args:
        p_prev: Position one sample before, ``(x, y, z)``.
        p_curr: Position at the sample of interest.
        p_next: Position one sample after.
        dt: Sample interval, seconds.

    Returns:
        ``(kappa, speed)``. ``kappa`` is 0 for a stationary or straight-line
        sample, where curvature is undefined or genuinely zero.
    """
    vx = (p_next[0] - p_prev[0]) / (2.0 * dt)
    vy = (p_next[1] - p_prev[1]) / (2.0 * dt)
    vz = (p_next[2] - p_prev[2]) / (2.0 * dt)
    speed = math.sqrt(vx * vx + vy * vy + vz * vz)

    # Below this the drone is effectively hovering; kappa = |v x a| / |v|^3
    # divides by speed cubed and would explode on numerical noise alone.
    if speed < 1e-6:
        return 0.0, speed

    ax = (p_next[0] - 2.0 * p_curr[0] + p_prev[0]) / (dt * dt)
    ay = (p_next[1] - 2.0 * p_curr[1] + p_prev[1]) / (dt * dt)
    az = (p_next[2] - 2.0 * p_curr[2] + p_prev[2]) / (dt * dt)

    cx = vy * az - vz * ay
    cy = vz * ax - vx * az
    cz = vx * ay - vy * ax
    cross = math.sqrt(cx * cx + cy * cy + cz * cz)

    return cross / (speed ** 3), speed


def validate_dynamics(
    choreo_data,
    limits: DynamicLimits = DEFAULT_DYNAMIC_LIMITS,
    dt: float = 0.1,
) -> dict:
    """Verify a show is dynamically feasible for the fleet to actually fly.

    Sweeps the timeline and asserts four bounds at every sample:

      * linear velocity ``|p_dot(t)| <= limits.max_velocity_ms``
      * centripetal acceleration ``kappa(t) * v(t)^2 <= limits.max_centripetal_ms2``
      * jerk ``|p_dddot(t)| <= limits.max_jerk_ms3``
      * pairwise separation ``min |p_i - p_j| >= limits.min_separation_m``

    This complements :func:`validate_choreography` rather than replacing it: that
    function enforces the shared airframe/abort envelope, this one enforces the
    tighter choreography-quality bounds described on :class:`DynamicLimits`.

    Args:
        choreo_data: A :class:`~tara_core.show.Show` or either supported dict
            shape.
        limits: Bounds to enforce.
        dt: Sweep interval, seconds.

    Returns:
        A report dict with ``status`` (``"PASSED"``/``"FAILED"``), ``summary``,
        ``metrics`` and ``violations`` — the same shape
        :func:`validate_choreography` returns, so the two can be presented
        interchangeably.
    """
    show = Show.from_any(choreo_data)

    violations: dict[str, list] = {
        "velocity": [],
        "centripetal": [],
        "jerk": [],
        "separation": [],
    }

    if not show.drones:
        return {
            "status": "FAILED",
            "summary": "No drones found in choreography.",
            "metrics": {
                "max_velocity_m_s": 0.0,
                "max_centripetal_m_s2": 0.0,
                "max_jerk_m_s3": 0.0,
                "min_distance_m": 0.0,
            },
            "violations": violations,
            "feasibility_certified": False,
        }

    times = sweep_times(show.duration, dt)

    # Sample every drone once up front. Velocity needs a neighbouring sample and
    # curvature needs both neighbours, so a single forward pass would either
    # re-interpolate the same instants repeatedly or miss the endpoints.
    tracks: dict[int, list] = {}
    for drone_id, wps in show.drones.items():
        track = []
        for t in times:
            wp = interpolate_waypoint(wps, t)
            track.append(None if wp is None else (wp["x"], wp["y"], wp["z"]))
        tracks[drone_id] = track

    max_vel = 0.0
    max_centripetal = 0.0
    max_jerk = 0.0
    min_dist = float("inf")

    for idx, t in enumerate(times):
        positions = {
            d_id: track[idx] for d_id, track in tracks.items() if track[idx] is not None
        }

        if len(positions) >= 2:
            dist = min_separation(positions)
            min_dist = min(min_dist, dist)
            if dist < limits.min_separation_m:
                violations["separation"].append({
                    "time": round(t, 2),
                    "distance": round(dist, 2),
                    "limit": limits.min_separation_m,
                })

        for d_id, track in tracks.items():
            curr = track[idx]
            if curr is None:
                continue

            # Speed from the backward difference, so the very first sample --
            # where there is no preceding position -- is simply skipped rather
            # than reported as an instantaneous jump from nowhere.
            if idx > 0 and track[idx - 1] is not None:
                prev = track[idx - 1]
                speed = math.dist(curr, prev) / dt
                max_vel = max(max_vel, speed)
                if speed > limits.max_velocity_ms:
                    violations["velocity"].append({
                        "time": round(t, 2),
                        "drone": d_id,
                        "velocity": round(speed, 2),
                        "limit": limits.max_velocity_ms,
                    })

            # Curvature is centred, so it needs a sample on each side.
            if 0 < idx < len(times) - 1 and track[idx - 1] is not None and track[idx + 1] is not None:
                kappa, centred_speed = _curvature(track[idx - 1], curr, track[idx + 1], dt)
                a_c = kappa * centred_speed * centred_speed
                max_centripetal = max(max_centripetal, a_c)
                if a_c > limits.max_centripetal_ms2:
                    violations["centripetal"].append({
                        "time": round(t, 2),
                        "drone": d_id,
                        "centripetal": round(a_c, 2),
                        "curvature": round(kappa, 4),
                        "velocity": round(centred_speed, 2),
                        "limit": limits.max_centripetal_ms2,
                    })

            # Jerk needs two samples on each side (a third-derivative central
            # stencil), same reasoning as curvature needing one on each side
            # for a second-derivative one.
            if (
                1 < idx < len(times) - 2
                and track[idx - 2] is not None
                and track[idx - 1] is not None
                and track[idx + 1] is not None
                and track[idx + 2] is not None
            ):
                j = jerk_from_positions(
                    track[idx - 2], track[idx - 1], track[idx + 1], track[idx + 2], dt
                )
                max_jerk = max(max_jerk, j)
                if j > limits.max_jerk_ms3:
                    violations["jerk"].append({
                        "time": round(t, 2),
                        "drone": d_id,
                        "jerk": round(j, 2),
                        "limit": limits.max_jerk_ms3,
                    })

    for key in violations:
        violations[key] = sorted(violations[key], key=lambda v: v["time"])[:_MAX_REPORTED]

    failed = any(violations[k] for k in violations)
    return {
        "status": "FAILED" if failed else "PASSED",
        "summary": (
            "Choreography is not dynamically feasible."
            if failed
            else "Choreography is dynamically feasible!"
        ),
        "metrics": {
            "max_velocity_m_s": round(max_vel, 2),
            "max_centripetal_m_s2": round(max_centripetal, 2),
            "max_jerk_m_s3": round(max_jerk, 2),
            "min_distance_m": round(min_dist, 2) if min_dist != float("inf") else 0.0,
        },
        "violations": violations,
        "feasibility_certified": not failed,
    }


def certify_feasibility(
    choreo_data,
    safety_limits: SafetyLimits = DEFAULT_LIMITS,
    dynamic_limits: DynamicLimits = DEFAULT_DYNAMIC_LIMITS,
) -> bool:
    """True iff a choreography clears the shared envelope and has bounded jerk.

    :func:`validate_choreography` enforces the shared airframe/abort envelope
    (proximity, velocity, acceleration, yaw rate) — the pre-#60 checks a
    certified show has always had to pass. This adds exactly one more gate on
    top of that: bounded jerk (:attr:`DynamicLimits.max_jerk_ms3`), which
    stands in for rotor spool-up lag (issue #60).

    Deliberately *not* required: the rest of :func:`validate_dynamics`'s own
    ``status`` (its ``velocity``/``centripetal`` bounds). Those are a
    separate, stricter choreography-quality check documented on
    :class:`DynamicLimits` as intentionally tighter than the airframe
    envelope — real generated shows routinely exceed them today (e.g. a
    plain 3-drone circle already reads ~4.4 m/s against its 4.0 m/s bound)
    without being unsafe or having a jerk problem. Folding that in here would
    make this gate block nearly every existing show, which is a different,
    much larger change than issue #60 asks for.

    Args:
        choreo_data: A :class:`~tara_core.show.Show` or either supported dict
            shape.
        safety_limits: Limits for the airframe/abort layer.
        dynamic_limits: Limits supplying ``max_jerk_ms3``.

    Returns:
        ``True`` if the safety layer passes and no jerk violations were found,
        ``False`` otherwise.
    """
    safety = validate_show(choreo_data, limits=safety_limits)
    dynamics = validate_dynamics(choreo_data, limits=dynamic_limits)
    return safety["status"] == "PASSED" and not dynamics["violations"]["jerk"]


def certify_feasibility_report(
    choreo_data,
    safety_limits: SafetyLimits = DEFAULT_LIMITS,
    dynamic_limits: DynamicLimits = DEFAULT_DYNAMIC_LIMITS,
) -> tuple[bool, dict]:
    """Canonical certification decision and complete report for flight-capable export.

    Enforces the single canonical flight-certification contract:
      1. Core safety limits (proximity, velocity, acceleration, yaw rate) must PASS.
      2. Dynamic feasibility jerk bound (|p_dddot| <= 20 m/s^3) must have 0 violations.

    Returns:
      (is_certified, report)
      where report is a dict containing status, summary, metrics, and violations
      (including jerk violations and max_jerk_m_s3).
    """
    safety = validate_show(choreo_data, limits=safety_limits)
    dynamics = validate_dynamics(choreo_data, limits=dynamic_limits)

    jerk_violations = dynamics.get("violations", {}).get("jerk", [])
    max_jerk = dynamics.get("metrics", {}).get("max_jerk_m_s3", 0.0)

    report = dict(safety)
    report["violations"] = dict(safety.get("violations", {}))
    report["violations"]["jerk"] = jerk_violations
    report["metrics"] = dict(safety.get("metrics", {}))
    report["metrics"]["max_jerk_m_s3"] = max_jerk

    core_passed = safety.get("status") == "PASSED"
    has_no_jerk = len(jerk_violations) == 0
    is_certified = core_passed and has_no_jerk
    report["feasibility_certified"] = is_certified

    if not is_certified:
        report["status"] = "FAILED"
        if core_passed and not has_no_jerk:
            report["summary"] = (
                f"Choreography failed feasibility certification: {len(jerk_violations)} jerk violation(s) "
                f"(peak {max_jerk:.1f} m/s^3 exceeds limit {dynamic_limits.max_jerk_ms3:.1f} m/s^3)."
            )

    return is_certified, report
