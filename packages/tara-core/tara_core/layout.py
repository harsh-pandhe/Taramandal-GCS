"""Launch-pad geometry for the fleet.

The original layout put every pad on a single line 2.0 m apart on Y, and gave
each drone a hover altitude 0.7 m above the previous one. Both numbers were
written before the safety limits existed and neither respects them:

  * a 0.7 m altitude step is below *both* the 0.8 m design threshold the Studio
    validator used and the 1.5 m limit the fleet aborts on, so a stack of drones
    hovering over their pads is already in violation before the show starts;
  * a single line does not scale — 100 drones 2 m apart is a 198 m row.

Pad spacing and altitude layering are therefore both derived from
:class:`~tara_core.safety.SafetyLimits` here, and a square-ish grid replaces the
line so the footprint grows as sqrt(n) instead of n.
"""

from __future__ import annotations

import math

from .safety import DEFAULT_LIMITS, SafetyLimits

#: Altitude of the lowest hover layer, metres above ground.
BASE_HOVER_ALT_M = 2.0

#: The largest fleet size the pad grid and altitude layers are sized for.
#: `tara_gcs.drone_manager` indexes a single grid built at this size (not one
#: sized to whatever fleet is currently connected) so that a drone's pad
#: never moves as the fleet grows — see `launch_grid`'s docstring. The Studio
#: generator (`tara_studio.generator._default_launch_state`) uses the exact
#: same fixed-size grid for its default spawn positions, which is what keeps
#: an auto-generated show's pad assignments identical to the GCS's — the two
#: previously disagreed past 32 drones (Studio placed everyone on a 1D line;
#: the GCS wrapped into a new row at 32), silently misplacing any show larger
#: than that.
MAX_FLEET_SIZE = 1000


def launch_grid(
    n: int,
    limits: SafetyLimits = DEFAULT_LIMITS,
    spacing: float | None = None,
) -> list[tuple[float, float]]:
    """Return ``n`` launch-pad ``(x, y)`` positions in the shared fleet frame.

    Pads are laid out row-major, anchored so that drone 0 sits at the origin,
    which keeps it usable as the fleet's local NED reference. Row width is
    ``min(ceil(sqrt(n)), floor(geofence_radius_m / spacing))`` — near-square as
    before for any ``n`` small enough that a square row already clears the
    geofence, but capped rather than left to grow past it.

    Both ``tara_studio.generator._default_launch_state`` (the Studio's default
    spawn layout) and ``tara_gcs.drone_manager._pad_offset`` (the live fleet's
    fixed per-drone pad, indexed into ``launch_grid(MAX_FLEET_SIZE)``) draw
    from this same function. Uncapped, a 1000-drone-sized grid needs 32
    columns, so drone id 16 — a low id, plausibly within a 50-drone show —
    landed at row 0, column 16: 32 m out on a single axis, exceeding
    ``DEFAULT_LIMITS.geofence_radius_m`` (30 m) at the moment a show starts,
    regardless of how many drones the show actually used. Capping the row
    width at ``floor(geofence_radius_m / spacing)`` (13, at the defaults)
    bounds how far a single row can reach before wrapping.

    The cap only changes behaviour once ``ceil(sqrt(n))`` would itself exceed
    that width (``n > 225`` at the defaults) — below that, including every
    ``n`` this module's and the Studio's own tests exercise, the grid is
    unchanged from the uncapped near-square layout. In particular every id
    below the row width (0..14 at the defaults) still lands on a single line
    at x=0, exactly as before: this is what keeps `sim/px4/launch_fleet.sh`'s
    real PX4 SITL spawn layout — a plain Y-axis line, matched against this
    grid only up to the fleet sizes real SITL is practical for — in agreement
    with `_pad_offset`'s fleet-frame conversion, and is why capping the row
    width (rather than a more compact but reordered layout) was chosen: it
    changes *only* the ids a straight line was already too wide for.

    Args:
        n: Number of drones.
        limits: Safety limits supplying the default spacing and the row-width
            cap.
        spacing: Pad separation in metres. Defaults to ``pad_spacing_m`` (issue
            #167 — deliberately looser than ``design_separation_m``, since a
            static pad has no tracking-error margin to absorb and can afford
            more room than the in-flight limit), which guarantees every pad
            clears the runtime abort threshold with margin, so drones may
            share a hover altitude safely.

    Returns:
        A list of ``n`` ``(x, y)`` tuples, metres.
    """
    if n < 0:
        raise ValueError(f"n must be non-negative, got {n}")
    spacing = limits.pad_spacing_m if spacing is None else spacing
    if spacing <= 0:
        raise ValueError(f"spacing must be positive, got {spacing}")

    max_cols = max(1, math.floor(limits.geofence_radius_m / spacing))
    cols = max(1, min(math.ceil(math.sqrt(n)), max_cols))
    return [
        (float(i // cols) * spacing, float(i % cols) * spacing) for i in range(n)
    ]


def altitude_layers(
    n: int,
    limits: SafetyLimits = DEFAULT_LIMITS,
    base: float = BASE_HOVER_ALT_M,
    step: float | None = None,
) -> list[float]:
    """Return ``n`` hover altitudes, metres above ground.

    When drones launch from :func:`launch_grid` they are already separated
    horizontally, so a shared altitude would be safe and this stagger is purely
    to reduce prop-wash interaction during a dense climb.

    Layers therefore *cycle* once the altitude ceiling is reached rather than
    growing without bound: at 100 drones a strictly increasing stack would reach
    200 m, well past ``max_altitude_m``. Two drones on the same layer are still
    separated by the grid.

    Args:
        n: Number of drones.
        limits: Safety limits supplying the step size and altitude ceiling.
        base: Altitude of the lowest layer.
        step: Vertical gap between layers. Defaults to ``design_separation_m``.

    Returns:
        A list of ``n`` altitudes, metres.
    """
    if n < 0:
        raise ValueError(f"n must be non-negative, got {n}")
    step = limits.design_separation_m if step is None else step
    if step <= 0:
        raise ValueError(f"step must be positive, got {step}")

    usable = limits.max_altitude_m - base
    max_layers = max(1, int(usable // step) + 1)
    return [base + step * (i % max_layers) for i in range(n)]
