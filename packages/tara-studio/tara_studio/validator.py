"""Choreography safety validation.

The implementation now lives in :mod:`tara_core.validate` so that the Studio,
the GCS uploader and the live fleet monitor all enforce one set of limits. This
module keeps the historical ``validate_choreography`` signature so existing
callers and tests continue to work.
"""

from __future__ import annotations

from tara_core import DEFAULT_LIMITS, SafetyLimits
from tara_core.geometry import interpolate_waypoint  # re-exported for callers
from tara_core.validate import validate_show

__all__ = ["interpolate_waypoint", "validate_choreography"]


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
