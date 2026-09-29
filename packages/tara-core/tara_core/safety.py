"""Single source of truth for every safety limit in the system.

Historically these numbers were duplicated in three places that disagreed:

  * ``tara_studio.validator``  used ``safe_dist_threshold = 0.8``
  * ``tara_gcs.trajectory_parser`` used ``safety_distance = 1.5``
  * ``tara_gcs.drone_manager``'s live proximity monitor also used ``1.5``

The design-time limit was therefore *looser* than the runtime abort limit, so
Studio would happily certify a show that the GCS then rejected at upload — or
worse, accepted and aborted mid-flight. That is backwards. A design-time check
is only meaningful if it is **stricter** than the limit that actually triggers
an abort, leaving margin for interpolation error and tracking error.

``SafetyLimits`` encodes that relationship and refuses to be constructed with
it inverted.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SafetyLimits:
    """Limits shared by the choreography validator, the uploader and the runtime.

    Attributes:
        min_separation_m: Hard runtime limit. The live proximity monitor triggers
            an RTL when two drones come closer than this. This is the number the
            fleet will actually abort on.
        design_separation_m: Design-time limit used when validating a show before
            it is ever uploaded. Must be >= ``min_separation_m``; the difference
            is the safety margin absorbing setpoint tracking error and the fact
            that a 10 Hz validation sweep can miss a closer approach between
            samples.
        max_velocity_ms: Per-drone speed ceiling.
        max_accel_ms2: Per-drone acceleration ceiling.
        max_yaw_rate_dps: Per-drone yaw rate ceiling, degrees/second.
        geofence_radius_m: Horizontal distance from the fleet origin beyond which
            a drone is considered to have escaped. Static fallback used only
            where no show is available to compute a dynamic envelope from
            (``layout.py``'s pad-grid capping, and any runtime context before
            a show is loaded) — see :meth:`geofence_radii` for the per-show
            derivation.
        max_altitude_m: Ceiling above ground.
        geofence_soft_warning_buffer_m: Margin added to a show's computed
            trajectory envelope (:meth:`tara_core.show.Show.geofence_envelope`)
            to get the soft-warning radius: a drone past this distance from
            home logs a warning but is not RTL'd.
        geofence_hard_buffer_m: Margin added to the same envelope to get the
            hard RTL radius. Larger than the soft buffer so a drone that trips
            the soft warning still has room to be tracking error, not an
            actual escape, before the hard boundary fires.
    """

    min_separation_m: float = 1.5
    design_separation_m: float = 2.0
    max_velocity_ms: float = 6.0
    max_accel_ms2: float = 50.0
    max_yaw_rate_dps: float = 300.0
    geofence_radius_m: float = 30.0
    max_altitude_m: float = 120.0
    geofence_soft_warning_buffer_m: float = 10.0
    geofence_hard_buffer_m: float = 15.0

    def __post_init__(self) -> None:
        if self.design_separation_m < self.min_separation_m:
            raise ValueError(
                "design_separation_m "
                f"({self.design_separation_m}) must be >= min_separation_m "
                f"({self.min_separation_m}): validating a show against a looser "
                "threshold than the one the fleet aborts on lets unsafe shows "
                "reach the flight line."
            )
        if self.geofence_hard_buffer_m < self.geofence_soft_warning_buffer_m:
            raise ValueError(
                "geofence_hard_buffer_m "
                f"({self.geofence_hard_buffer_m}) must be >= "
                f"geofence_soft_warning_buffer_m ({self.geofence_soft_warning_buffer_m}): "
                "the hard RTL radius must not be tighter than the soft-warning "
                "radius around the same envelope."
            )
        for name in (
            "min_separation_m",
            "design_separation_m",
            "max_velocity_ms",
            "max_accel_ms2",
            "max_yaw_rate_dps",
            "geofence_radius_m",
            "max_altitude_m",
            "geofence_soft_warning_buffer_m",
            "geofence_hard_buffer_m",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive, got {getattr(self, name)}")

    @property
    def separation_margin_m(self) -> float:
        """Headroom between the design limit and the runtime abort limit."""
        return self.design_separation_m - self.min_separation_m

    def geofence_radii(self, envelope_radius_m: float) -> tuple[float, float]:
        """Derive ``(soft_warning_radius_m, hard_geofence_radius_m)`` for a show.

        Both are the show's computed trajectory envelope
        (:meth:`tara_core.show.Show.max_horizontal_radius_m` — full
        entry-blend + show + exit, not just launch-pad positions) plus a
        fixed buffer absorbing setpoint tracking error. The hard radius uses
        the larger buffer so it always sits strictly outside the soft one.

        Args:
            envelope_radius_m: The show's max horizontal distance from origin
                across every waypoint of every drone.

        Returns:
            ``(soft_warning_radius_m, hard_geofence_radius_m)``, metres.
        """
        if envelope_radius_m < 0:
            raise ValueError(
                f"envelope_radius_m must be non-negative, got {envelope_radius_m}"
            )
        return (
            envelope_radius_m + self.geofence_soft_warning_buffer_m,
            envelope_radius_m + self.geofence_hard_buffer_m,
        )


#: The limits every package uses unless explicitly overridden.
DEFAULT_LIMITS = SafetyLimits()
