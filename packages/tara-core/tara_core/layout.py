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


def launch_grid(
    n: int,
    limits: SafetyLimits = DEFAULT_LIMITS,
    spacing: float | None = None,
) -> list[tuple[float, float]]:
    """Return ``n`` launch-pad ``(x, y)`` positions in the shared fleet frame.

    Pads are laid out row-major on a near-square grid anchored so that drone 0
    sits at the origin, which keeps it usable as the fleet's local NED reference.

    Args:
        n: Number of drones.
        limits: Safety limits supplying the default spacing.
        spacing: Pad separation in metres. Defaults to ``design_separation_m``,
            which guarantees every pad clears the runtime abort threshold with
            margin, so drones may share a hover altitude safely.

    Returns:
        A list of ``n`` ``(x, y)`` tuples, metres.
    """
    if n < 0:
        raise ValueError(f"n must be non-negative, got {n}")
    spacing = limits.design_separation_m if spacing is None else spacing
    if spacing <= 0:
        raise ValueError(f"spacing must be positive, got {spacing}")

    cols = max(1, math.ceil(math.sqrt(n)))
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
