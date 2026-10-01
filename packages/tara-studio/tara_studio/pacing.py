"""Drone-count-aware pacing for the generated shows.

Every generator in :mod:`tara_studio.generator` used to hardcode the same
timeline regardless of fleet size: 5 s of takeoff, a 2 s blend from the pad
into the formation, a 20 s choreography window, and whatever was left over for
landing. Those numbers were chosen when a "show" meant three drones, and they
do not survive a larger fleet, because the *distances* each phase has to cover
grow with the drone count while the time allowed stayed constant:

  * **Climb.** ``tara_core.altitude_layers`` stacks drones on separate hover
    layers, cycling up to ``max_altitude_m``. At 3 drones the top layer is 6 m
    (1.2 m/s over the fixed 5 s); at 100 drones it is 120 m, i.e. 24 m/s — four
    times the 6 m/s speed ceiling, before the drones have even finished taking
    off.
  * **Blend.** The formation centre sits at ``y = num_drones - 1`` and the
    launch grid spreads over ~sqrt(n) rows, so the pad-to-formation distance
    grows roughly linearly with n. At 100 drones the farthest drone covers
    ~103 m in the fixed 2 s window: 51 m/s.
  * **Return.** Landing retraces that same distance in the leftover window.

The result was that a freshly generated 100-drone show failed validation on
velocity, acceleration and yaw rate — and, the part that makes this a
generator bug rather than an operator inconvenience, the operator could not
fix it by stretching the timeline, because the phase boundaries were literal
constants (``if t <= 5.0`` / ``elif t <= 25.0``). Raising ``duration`` from
30 s to 120 s only stretched the *landing* window; peak velocity stayed at
51 m/s.

Pacing is therefore computed here, from the geometry the fleet actually has to
traverse. Each phase gets the time it needs at a target cruise speed::

    phase_seconds = phase_distance / cruise_speed

with ``cruise_speed`` a fraction of ``max_velocity_ms`` (see
:data:`CRUISE_FRACTION`) so a show is paced with headroom rather than riding
the ceiling. Phases are then floored at their historical durations, so a small
fleet is never paced *slower* than it used to be — the N=3 and N=10 timelines
come out bit-for-bit unchanged.

The multiplier this implies (:attr:`ShowPacing.multiplier`) is the ratio of
the paced timeline to the historical one: 1.0 for a fleet small enough to need
no stretching, growing with n as the geometry demands it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from tara_core import DEFAULT_LIMITS, MAX_FLEET_SIZE, SafetyLimits

#: Historical phase durations, in seconds. Every generator used these as
#: literals. They are now the *floor* a paced show may not go below, which is
#: what keeps small-N shows exactly as brisk as they have always been.
BASE_TAKEOFF_S = 5.0
BASE_BLEND_S = 2.0
BASE_CHOREO_S = 20.0
BASE_LAND_S = 5.0

#: Fraction of ``max_velocity_ms`` the pacing aims for.
#:
#: The phases are eased rather than linear (see ``generator._ease``), so a phase
#: that covers ``d`` metres in ``t`` seconds peaks above its mean speed, not
#: at it. ``_ease`` was a smoothstep ramp (peak = 1.5x mean) until issue #60
#: replaced it with ``tara_core.geometry.quintic_profile``'s minimum-jerk ramp,
#: which also zeros *acceleration* at both ends (smoothstep only zeroed
#: velocity, which left acceleration jumping at every phase boundary — a jerk
#: discontinuity). That extra flatness at the ends comes out of the middle:
#: the quintic ramp's peak velocity is 1.875x its mean (``30*0.5^4-60*0.5^3
#: +30*0.5^2 = 1.875`` vs. smoothstep's ``6*0.5*(1-0.5) = 1.5``), 25% higher
#: for the same phase duration. Budgeting at half the ceiling therefore now
#: overshoots it; CRUISE_FRACTION is scaled down by the same 1.5/1.875 ratio
#: (0.5 -> 0.4) so the peak still lands right around the ceiling, preserving
#: the margin the rest of this comment always relied on: the per-axis
#: oscillations the shapes add on top of the blend (the helix's +/-1.2 m
#: height wobble, the cube's +/-1 m sway), and the fact that the validator
#: measures speed by finite difference over a 0.1 s window, which reads
#: slightly high wherever a trajectory curves.
#:
#: 0.4 keeps a 3-drone show on exactly the historical 5 s / 2 s / 20 s / 5 s
#: timeline, same as 0.5 did before the ramp changed — small-N shows fly
#: comfortably under either ceiling, so nothing about them moves.
CRUISE_FRACTION = 0.4

#: Fraction of ``max_yaw_rate_dps`` the choreography's rotation aims for. The
#: shapes drive yaw from the orbital angle, so the choreography window has to
#: be long enough for its revolutions to stay under the yaw-rate ceiling as
#: well as under the speed ceiling.
CRUISE_YAW_FRACTION = 0.55


@dataclass(frozen=True)
class ShowPacing:
    """The timeline a generated show should run on for a given fleet size.

    Attributes:
        takeoff_s: Length of the vertical climb phase.
        blend_s: Length of the pad-to-formation blend. It overlaps the start of
            the choreography phase, as it always has.
        choreo_s: Length of the choreography phase.
        land_s: Length of the landing phase.
    """

    takeoff_s: float
    blend_s: float
    choreo_s: float
    land_s: float

    @property
    def choreo_start_s(self) -> float:
        """Absolute time the choreography phase begins."""
        return self.takeoff_s

    @property
    def land_start_s(self) -> float:
        """Absolute time the landing phase begins."""
        return self.takeoff_s + self.choreo_s

    @property
    def total_s(self) -> float:
        """Total show length implied by the phases."""
        return self.takeoff_s + self.choreo_s + self.land_s

    @property
    def multiplier(self) -> float:
        """How much slower this timeline is than the historical fixed one.

        1.0 means "not slowed at all", which is what every fleet small enough
        to fly the original timeline safely still gets.
        """
        base = BASE_TAKEOFF_S + BASE_CHOREO_S + BASE_LAND_S
        return self.total_s / base


def formation_spread(start_positions: dict, targets: dict) -> float:
    """Longest pad-to-formation distance across the fleet, metres.

    This is the distance that actually drives the blend's speed: every drone
    covers its own pad-to-target gap in the same window, so the window has to
    be sized for the worst one.

    Args:
        start_positions: ``{drone_id: {"x", "y", "z"}}`` launch pads.
        targets: ``{drone_id: (x, y, z)}`` formation position each drone blends
            towards, in the generator's NED convention (negative z is up).

    Returns:
        The maximum Euclidean distance, or 0.0 for an empty fleet.
    """
    worst = 0.0
    for d_id, pad in start_positions.items():
        tx, ty, tz = targets[d_id]
        worst = max(worst, math.dist((pad["x"], pad["y"], pad["z"]), (tx, ty, tz)))
    return worst


def plan_pacing(
    num_drones: int,
    climb_m: float,
    spread_m: float,
    revolutions: float = 1.0,
    limits: SafetyLimits = DEFAULT_LIMITS,
) -> ShowPacing:
    """Derive a fleet-size-aware timeline from the distances a show must cover.

    Args:
        num_drones: Fleet size, validated against ``MAX_FLEET_SIZE``. The
            pacing itself is driven by the distances rather than by n directly,
            since that is what sets the speed a drone has to fly — two
            50-drone shows with different formation extents genuinely need
            different pacing.
        climb_m: Highest hover altitude any drone must reach, metres.
        spread_m: Longest pad-to-formation distance any drone must cover during
            the blend, metres. The landing return retraces it.
        revolutions: How many full turns the choreography's yaw makes. The
            circle turns once, the helix 1.5x, the heart 0.25x; the cube and
            spline hold a fixed heading and pass 0.
        limits: Safety limits supplying the speed and yaw-rate ceilings.

    Returns:
        A :class:`ShowPacing` whose every phase is at least its historical
        length, so small fleets are unaffected.

    Raises:
        ValueError: If ``num_drones`` is outside ``1..MAX_FLEET_SIZE``, or a
            distance or revolution count is negative.
    """
    if not 1 <= num_drones <= MAX_FLEET_SIZE:
        raise ValueError(
            f"num_drones must be between 1 and {MAX_FLEET_SIZE}, got {num_drones}"
        )
    for name, value in (
        ("climb_m", climb_m),
        ("spread_m", spread_m),
        ("revolutions", revolutions),
    ):
        if value < 0:
            raise ValueError(f"{name} must be non-negative, got {value}")

    cruise = limits.max_velocity_ms * CRUISE_FRACTION

    # Climb and blend are straight-line traversals: time = distance / speed.
    takeoff_s = max(BASE_TAKEOFF_S, climb_m / cruise)
    blend_s = max(BASE_BLEND_S, spread_m / cruise)

    # The choreography's own motion — orbiting a fixed-radius formation — is
    # independent of fleet size and already comfortably legal at the base 20 s,
    # so the window is NOT scaled by n. It only has to (a) contain the blend
    # with the historical amount of formation time left over after it, and
    # (b) be long enough for its rotation to clear the yaw-rate ceiling.
    yaw_s = 0.0
    if revolutions > 0:
        yaw_s = (360.0 * revolutions) / (limits.max_yaw_rate_dps * CRUISE_YAW_FRACTION)
    hold_s = BASE_CHOREO_S - BASE_BLEND_S
    choreo_s = max(BASE_CHOREO_S, blend_s + hold_s, yaw_s)

    # Landing retraces the blend distance and then descends from the top layer.
    # _land_waypoint splits the window at LAND_RETURN_FRACTION, so the window
    # must be long enough that *each* sub-phase clears its own distance within
    # its own share of it.
    from .generator import LAND_RETURN_FRACTION

    return_s = (spread_m / cruise) / LAND_RETURN_FRACTION
    descend_s = (climb_m / cruise) / (1.0 - LAND_RETURN_FRACTION)
    land_s = max(BASE_LAND_S, return_s, descend_s)

    return ShowPacing(
        takeoff_s=takeoff_s,
        blend_s=blend_s,
        choreo_s=choreo_s,
        land_s=land_s,
    )
