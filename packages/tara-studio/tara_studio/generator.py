"""
Taramandal Studio - Trajectory Generation Library
"""

import bisect
import functools
import math

from tara_core import DEFAULT_LIMITS, MAX_FLEET_SIZE, altitude_layers, launch_grid
from tara_core.assignment import optimal_assignment

from .pacing import ShowPacing, formation_spread, plan_pacing


def _default_launch_state(
    num_drones: int, spawn_offsets: list | None, target_heights: list | None
) -> tuple[list, list]:
    """Fill in default launch pads and hover altitudes from the shared limits.

    The hover altitudes previously stepped by 0.7 m per drone, which is below
    both the design threshold and the 1.5 m the fleet aborts on. That was not
    theoretical: in a 3-drone circle the drones converge horizontally as they
    blend out of takeoff, leaving the vertical stagger as the *only* separation,
    and the show bottomed out at 0.77 m. Altitude steps now come from
    ``tara_core.altitude_layers``, which derives them from the safety limits.

    Default pad positions come from the SAME fixed-size grid
    (``tara_core.launch_grid(MAX_FLEET_SIZE)``) that
    ``tara_gcs.drone_manager._pad_offset`` uses, not a 1D line. Those two used
    to disagree past 32 drones — a line keeps every drone at x=0 forever,
    while the GCS's grid wraps into a new row at 32 columns — which would
    silently misplace any auto-generated show larger than that once it
    reached the GCS. Returns ``(x, y)`` pairs when defaulted.

    An explicitly-passed ``spawn_offsets`` (the Studio UI's per-drone Y-offset
    field) is honoured as-is for backward compatibility — a flat list of
    floats, x=0 — since that is a public API shape (studio/server.py's
    ``GenerateRequest.spawn_offsets``, the web app's ``spawnOffsets`` state).
    Only the *default* (no override given) case gets the corrected 2D grid.

    Both overrides are indexed per drone id by every generator, so a list
    shorter than ``num_drones`` used to fail with a bare ``IndexError`` out of
    the middle of a shape's waypoint loop — the API surfaced that as an opaque
    500 with no indication of which field was wrong. Since a designer only
    hand-places pads for a fleet they have already sized, a short list is a
    caller bug, not something to pad with defaults: silently filling the tail
    from the grid would hand back a show whose pads disagree with the ones the
    operator actually set. Both are therefore length-checked up front, and
    reported by name and by the lengths involved.

    Raises:
        ValueError: If ``num_drones`` is outside ``1..MAX_FLEET_SIZE``, or if a
            supplied override's length does not match ``num_drones``.
    """
    if not 1 <= num_drones <= MAX_FLEET_SIZE:
        raise ValueError(
            f"num_drones must be between 1 and {MAX_FLEET_SIZE}, got {num_drones}"
        )

    for name, override in (("spawn_offsets", spawn_offsets), ("target_heights", target_heights)):
        if override is not None and len(override) != num_drones:
            raise ValueError(
                f"{name} has {len(override)} entries but num_drones is {num_drones}; "
                f"supply exactly one per drone or omit it to use the default grid"
            )

    if spawn_offsets is None:
        spawn_offsets = launch_grid(MAX_FLEET_SIZE)[:num_drones]
    if target_heights is None:
        target_heights = altitude_layers(num_drones)
    return spawn_offsets, target_heights


#: Fraction of the landing window spent returning to the pad horizontally
#: before any descent begins. See _land_waypoint's docstring for why.
LAND_RETURN_FRACTION = 0.6


def _land_waypoint(d_id: int, ratio: float, start_positions: dict, target_heights: dict, land_entry: dict) -> dict:
    """Computes one drone's position during the landing window.

    Splits the window into two sequential sub-phases instead of blending
    horizontal return and vertical descent simultaneously:

      1. **Return** (first ``LAND_RETURN_FRACTION`` of the window): fly from
         the choreography's end position to this drone's own pad, holding
         the choreography's ending altitude.
      2. **Descend** (the rest): drop straight down to the ground, XY already
         fixed at the pad.

    Blending both at once let one drone's horizontal return path and
    another's converge in space at the same moment their altitude gap was
    also shrinking, which measured as low as 1.09m of total separation for
    the circle shape — under the 1.5m the fleet aborts on — even though the
    launch-pad grid itself is spaced >=2m apart. Returning to the
    already-safe pad grid *before* giving up altitude means the altitude
    collapse at touchdown always has that >=2m of horizontal separation to
    fall back on.

    Args:
        d_id: Drone index.
        ratio: Progress through the landing window, 0 to 1.
        start_positions: Each drone's pad ``{"x", "y"}``.
        target_heights: Each drone's hover altitude.
        land_entry: Each drone's ``{"x", "y", "z"}`` at the moment landing
            began (the choreography's last waypoint) — captured once, not
            re-read from the continuously-updating last-appended waypoint.

    Returns:
        ``{"x", "y", "z", "yaw"}``, unrounded.
    """
    return_ratio = _ease(ratio / LAND_RETURN_FRACTION)
    descend_ratio = _ease((ratio - LAND_RETURN_FRACTION) / (1.0 - LAND_RETURN_FRACTION))

    entry = land_entry[d_id]
    target_x = start_positions[d_id]["x"]
    target_y = start_positions[d_id]["y"]

    x_val = entry["x"] + return_ratio * (target_x - entry["x"])
    y_val = entry["y"] + return_ratio * (target_y - entry["y"])
    z_val = entry["z"] * (1.0 - descend_ratio)

    # Yaw unwinds to 0 across the return leg rather than snapping there on the
    # first landing sample. A drone that ended the choreography facing 178 deg
    # used to jump to 0 between two samples — 356 deg/s at 2 Hz sampling, over
    # the 300 deg/s ceiling — regardless of how long the landing window was.
    yaw_val = _blend_yaw(entry.get("yaw", 0.0), 0.0, return_ratio)

    return {"x": x_val, "y": y_val, "z": z_val, "yaw": yaw_val}


def _pad_xy(pad) -> tuple[float, float]:
    """Extracts (x, y) from either pad representation `_default_launch_state` can return.

    A defaulted pad is an ``(x, y)`` tuple from ``launch_grid``; an explicitly
    passed ``spawn_offsets`` entry is a bare float (y-only, x=0) for backward
    compatibility with the existing Studio UI/API.
    """
    if isinstance(pad, (tuple, list)):
        return float(pad[0]), float(pad[1])
    return 0.0, float(pad)


def _ease(ratio: float) -> float:
    """Smoothstep: a 0->1 ramp that starts and ends at zero rate of change.

    Phase transitions used to be linear ramps, which meant the drone's velocity
    stepped discontinuously at every phase boundary — from a standstill to full
    blend speed between two samples. Velocity/dt is acceleration, so those steps
    read as acceleration spikes no matter how long the phases are, which is
    exactly the kind of violation extra show time cannot fix. Easing the ramp
    makes velocity go to zero at both ends of each phase instead.
    """
    r = min(1.0, max(0.0, ratio))
    return r * r * (3.0 - 2.0 * r)


def _blend_yaw(start_deg: float, target_deg: float, ratio: float) -> float:
    """Interpolate a heading the short way round, in degrees.

    Rotating 350 deg -> 10 deg has to cross 0, not sweep 340 deg backwards, so
    the difference is wrapped into (-180, 180] before it is scaled.

    Only valid for a *fixed* target. Interpolating towards a target that itself
    moves (a drone's formation heading during the blend, say) has to be done on
    the unwrapped angle instead — recomputing the short way round per sample
    makes the interpolated heading reverse direction the moment the shortest
    path flips at 180 deg, which is a discontinuity of its own. See
    ``_run_show``, which blends the unwrapped yaw and wraps once at the end.
    """
    delta = (target_deg - start_deg + 180.0) % 360.0 - 180.0
    return _wrap_heading(start_deg + delta * ratio)


def _wrap_heading(deg: float) -> float:
    """Wrap a heading into [0, 360) at 0.1 deg resolution.

    Rounding has to happen before the final wrap: 359.96 wraps to itself and
    then rounds up to 360.0, which is outside the show schema's [0, 360) yaw
    range (issue #55). The second ``% 360`` only ever changes that one value;
    anything that rounded below 360 passes through it unchanged.
    """
    return round(deg % 360.0, 1) % 360.0


def _launch_state(num_drones: int, spawn_offsets, target_heights) -> tuple[dict, dict]:
    """Resolve pads and hover altitudes into the two per-drone dicts every shape uses."""
    spawn_offsets, target_heights = _default_launch_state(
        num_drones, spawn_offsets, target_heights
    )
    start_positions = {
        d_id: {
            "x": _pad_xy(spawn_offsets[d_id])[0],
            "y": _pad_xy(spawn_offsets[d_id])[1],
            "z": 0.0,
        }
        for d_id in range(num_drones)
    }
    heights = {d_id: float(target_heights[d_id]) for d_id in range(num_drones)}
    return start_positions, heights


def plan_show_pacing(num_drones: int, shape: str, spawn_offsets: list = None, target_heights: list = None) -> ShowPacing:
    """Return the pacing a generated show will use, without generating it.

    Exposed so callers can report or preview the timeline (the Studio API
    returns it alongside the show) rather than having to infer it from the
    waypoints. This is the same call the generators make internally, so what it
    reports is what they will produce.

    Args:
        num_drones: Fleet size.
        shape: One of the supported shape names.
        spawn_offsets: Optional per-drone pad override.
        target_heights: Optional per-drone hover altitude override.

    Returns:
        The :class:`~tara_studio.pacing.ShowPacing` for that show.

    Raises:
        ValueError: If ``shape`` is not a supported shape name.
    """
    spec = _SHAPES.get(shape.lower())
    if spec is None:
        raise ValueError(f"unknown shape {shape!r}; expected one of {sorted(_SHAPES)}")
    start_positions, heights = _launch_state(num_drones, spawn_offsets, target_heights)
    return _pacing_for(spec, num_drones, start_positions, heights)


def _pacing_for(spec, num_drones: int, start_positions: dict, target_heights: dict) -> ShowPacing:
    """Measure a shape's geometry at this fleet size and plan the timeline for it.

    The two distances that drive pacing are read off the shape itself rather
    than assumed: ``climb_m`` is the tallest hover layer in play, and
    ``spread_m`` is the longest pad-to-formation gap, evaluated at the moment
    the blend ends (``ratio=0``, where every shape places its drones at the
    formation's entry positions). That keeps a shape whose formation happens to
    sit close to the pads from being paced as slowly as one that flings the
    fleet across the arena at the same drone count.
    """
    entry_targets = {
        d_id: spec.formation(d_id, num_drones, 0.0, target_heights[d_id])
        for d_id in range(num_drones)
    }
    # The blend starts from where takeoff leaves each drone — over its pad, at
    # its hover altitude — not from the pad on the ground, so that is the point
    # the spread is measured from.
    hover_positions = {
        d_id: {"x": pad["x"], "y": pad["y"], "z": -target_heights[d_id]}
        for d_id, pad in start_positions.items()
    }
    climb_m = max(target_heights.values()) if target_heights else 0.0
    spread_m = formation_spread(hover_positions, entry_targets)
    return plan_pacing(
        num_drones,
        climb_m=climb_m,
        spread_m=spread_m,
        revolutions=spec.revolutions,
    )


def _run_show(spec, num_drones: int, duration: float, rate: float, spawn_offsets, target_heights) -> dict:
    """Generate one show, pacing its phases for the fleet size.

    Replaces the identical takeoff/choreography/landing loop the five shape
    functions each carried a copy of. The loop itself is unchanged in
    structure; what changed is that the phase boundaries come from
    :func:`~tara_studio.pacing.plan_pacing` instead of the literals 5.0/25.0,
    and the timeline runs for however long the paced phases need rather than
    for whatever the caller passed as ``duration``.

    ``duration`` is still honoured as a *lower* bound: a caller asking for a
    longer show than the pacing requires gets the extra time, spent on the
    choreography (the part worth watching), exactly as a 30 s three-drone show
    behaves today. What it can no longer do is make the show shorter than the
    fleet can safely fly — a 100-drone show asked to finish in 30 s comes back
    at the ~78 s it actually needs, because the alternative is handing back a
    show that fails validation and cannot be fixed by any parameter the
    operator controls.

    Args:
        spec: The :class:`_ShapeSpec` describing this formation.
        num_drones: Fleet size.
        duration: Requested show length, seconds. Treated as a floor.
        rate: Waypoint sampling rate, Hz.
        spawn_offsets: Optional per-drone pad override.
        target_heights: Optional per-drone hover altitude override.

    Returns:
        ``{"drones": [{"id", "waypoints"}, ...]}``.
    """
    start_positions, heights = _launch_state(num_drones, spawn_offsets, target_heights)
    pacing = _pacing_for(spec, num_drones, start_positions, heights)

    takeoff_s = pacing.takeoff_s
    blend_s = pacing.blend_s
    land_s = pacing.land_s
    # Spend any surplus the caller asked for on the choreography.
    choreo_s = max(pacing.choreo_s, duration - takeoff_s - land_s)

    choreo_start = takeoff_s
    land_start = takeoff_s + choreo_s
    total_s = land_start + land_s

    drones_wps = {d_id: [] for d_id in range(num_drones)}
    total_steps = int(round(total_s * rate))

    land_entry = None  # captured once, at the moment landing begins
    for step in range(total_steps + 1):
        t = step / float(rate)

        if t <= takeoff_s:
            ratio = _ease(t / takeoff_s)
            for d_id in range(num_drones):
                z_val = -(heights[d_id] * ratio)
                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": start_positions[d_id]["x"],
                    "y": start_positions[d_id]["y"],
                    "z": round(z_val, 2),
                    "yaw": 0.0,
                })
        elif t <= land_start:
            elapsed = t - choreo_start
            ratio = elapsed / choreo_s
            blend_ratio = _ease(elapsed / blend_s)

            for d_id in range(num_drones):
                target_x, target_y, target_z = spec.formation(
                    d_id, num_drones, ratio, heights[d_id]
                )
                # Yaw is blended over the same window as position. Each drone's
                # formation heading starts at its own angle offset around the
                # ring, so snapping straight to it on the first choreography
                # step made the drone half-way round the ring rotate 180 deg
                # between two samples — a yaw-rate violation that grew with n
                # as the offsets spread out, and one no amount of extra show
                # time could fix, since it happened *at* the phase boundary.
                #
                # The scaling is applied to the shape's *unwrapped* angle and
                # wrapped exactly once here. Wrapping first and then blending
                # towards the wrapped value makes the heading reverse whenever
                # the shortest path flips sign at 180 deg.
                yaw_val = _wrap_heading(blend_ratio * spec.yaw(d_id, num_drones, ratio))

                pad = start_positions[d_id]
                x_val = pad["x"] + blend_ratio * (target_x - pad["x"])
                y_val = pad["y"] + blend_ratio * (target_y - pad["y"])
                # z blends out of the altitude takeoff actually reached, rather
                # than being taken from the formation directly. The cube is the
                # shape that made this matter: its grid slots carry their own
                # altitudes, unrelated to the hover layer a drone just climbed
                # to, so the first choreography sample used to teleport a drone
                # several metres vertically in one step.
                climb_z = -heights[d_id]
                z_val = climb_z + blend_ratio * (target_z - climb_z)

                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": round(x_val, 2),
                    "y": round(y_val, 2),
                    "z": round(z_val, 2),
                    "yaw": yaw_val,
                })
        else:
            elapsed = t - land_start
            ratio = min(1.0, elapsed / land_s)

            if land_entry is None:
                land_entry = {d_id: dict(drones_wps[d_id][-1]) for d_id in range(num_drones)}

            for d_id in range(num_drones):
                wp = _land_waypoint(d_id, ratio, start_positions, heights, land_entry)
                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": round(wp["x"], 2),
                    "y": round(wp["y"], 2),
                    "z": round(wp["z"], 2),
                    "yaw": wp["yaw"],
                })

    return {"drones": [{"id": d_id, "waypoints": wps} for d_id, wps in drones_wps.items()]}


class _ShapeSpec:
    """One formation: where each drone sits over the choreography, and its heading.

    Args:
        formation: ``(d_id, n, ratio, height) -> (x, y, z)``, the drone's
            position in the formation at ``ratio`` through the choreography.
            ``z`` is NED (negative up). Called at ``ratio=0`` during pacing to
            measure how far the fleet has to travel to enter the formation.
        yaw: ``(d_id, n, ratio) -> degrees``, **unwrapped** — an orbiting shape
            returns 0, 90, ..., 360, 450 rather than wrapping back to 0.
            ``_run_show`` scales this by the blend ratio and wraps into
            ``[0, 360)`` exactly once, at the end. A pre-wrapped value cannot be
            blended: the interpolation would reverse direction each time the
            shortest path between the current and target heading flips at
            180 deg.
        revolutions: Full turns the heading makes across the choreography, used
            to keep the yaw rate legal. 0 for shapes that hold a heading.
    """

    def __init__(self, formation, yaw, revolutions: float):
        self.formation = formation
        self.yaw = yaw
        self.revolutions = revolutions


def _circle_radius(n: int) -> float:
    """Ring radius large enough that adjacent drones clear design_separation_m
    horizontally on their own, without leaning on the altitude-layer stagger
    for the margin the ring itself should provide.

    Fixed at 4.0 m regardless of n, the ring's adjacent-drone chord shrinks as
    ``2*4*sin(pi/n)`` -- already only ~0.50 m at n=50, and never more than
    that at any n -- so every fleet size relied entirely on the per-drone
    altitude layer for proximity margin. That margin is real in 3D, but
    horizontal-only distance (ignoring z, i.e. what a purely horizontal sensor
    or a same-layer pair actually sees) measured as low as ~0.004 m during
    entry-blend at n=50, between two drones that happened to share a layer.
    Solving ``2*r*sin(pi/n) == design_separation_m`` for r keeps that chord
    exactly at the threshold at rest; the 1.2x margin covers points closing in
    further than their steady-state gap while blending in from the launch
    grid. Floored at the original 4.0 m so small fleets, where this was never
    tight, are unaffected. ``n=1`` has no "adjacent drone" at all, and
    ``sin(pi/1)`` is only zero to floating-point rounding, so it is excluded
    rather than fed through the formula -- otherwise the near-zero
    denominator sends the radius (and everything paced off it) to the
    billions.
    """
    if n <= 1:
        return 4.0
    min_r = (DEFAULT_LIMITS.design_separation_m / 2.0) / math.sin(math.pi / n)
    return max(4.0, 1.2 * min_r)


def _circle_formation(d_id: int, n: int, ratio: float, height: float):
    radius = _circle_radius(n)
    angle = (2.0 * math.pi * d_id) / n + (2.0 * math.pi * ratio)
    return (
        5.0 + radius * math.cos(angle),
        float(n - 1) + radius * math.sin(angle),
        -height,
    )


def _circle_yaw(d_id: int, n: int, ratio: float) -> float:
    """Unwrapped heading, degrees — see _ShapeSpec.yaw on why it is not wrapped."""
    angle = (2.0 * math.pi * d_id) / n + (2.0 * math.pi * ratio)
    return math.degrees(angle)


def _helix_formation(d_id: int, n: int, ratio: float, height: float):
    angle = (2.0 * math.pi * d_id) / n + (2.0 * math.pi * ratio * 1.5)
    # Shared across every drone -- no d_id term -- rather than the previous
    # per-drone phase offset (`+ d_id`). The ring (radius 5) only spaces
    # adjacent drones 0.3-0.6 m apart horizontally at N=50/100, so the entire
    # show relied on the altitude-layer stagger (2 m between adjacent layers)
    # for real separation. A per-drone phase meant adjacent drones' oscillations
    # were up to 1 rad out of phase, and `1.2 * (sin(phi) - sin(phi+1))` swings
    # up to ~1.15 m -- enough to periodically cancel most of that 2 m stagger,
    # which is what actually produced the ~0.85-1.06 m separations measured
    # across blend/choreo/landing. Applying the *same* oscillation to every
    # drone is a common shift, like the cube's sway term: it moves the whole
    # ring's altitude up and down together but never touches the difference
    # between any two drones' heights, so the layer stagger is exactly 2 m at
    # every instant instead of only on average. Amplitude is also reduced from
    # 1.2 to 0.4 m so it can never trigger the `max(1.5, ...)` floor below --
    # the lowest hover layer is 2.0 m, and 2.0 - 0.4 = 1.6 stays clear of it;
    # letting the floor clip only some drones' altitudes was exactly the kind
    # of per-drone inconsistency this fix removes everywhere else.
    height_osc = 0.4 * math.sin(2.0 * math.pi * ratio)
    return (
        6.0 + 5.0 * math.cos(angle),
        float(n - 1) + 5.0 * math.sin(angle),
        -max(1.5, height + height_osc),
    )


def _helix_yaw(d_id: int, n: int, ratio: float) -> float:
    """Unwrapped heading, degrees — see _ShapeSpec.yaw on why it is not wrapped."""
    angle = (2.0 * math.pi * d_id) / n + (2.0 * math.pi * ratio * 1.5)
    return math.degrees(angle)


def _heart_xy(theta: float, scale: float = 0.25) -> tuple[float, float]:
    """The classic heart curve, scaled down to typical arena bounds (approx -4 to +4) at the default scale."""
    raw_x = 16.0 * (math.sin(theta) ** 3)
    raw_y = (
        13.0 * math.cos(theta)
        - 5.0 * math.cos(2.0 * theta)
        - 2.0 * math.cos(3.0 * theta)
        - math.cos(4.0 * theta)
    )
    return raw_x * scale, raw_y * scale


@functools.lru_cache(maxsize=1)
def _heart_arc_length_table(samples: int = 4000) -> tuple[list[float], list[float], float]:
    """``(thetas, cumulative arc length at each, total perimeter)``, at the
    curve's *raw* (``scale=1.0``) size.

    Arc-length *fractions* are scale-invariant, so this table -- built once,
    independent of ``n`` -- is what every fleet size's theta lookup shares.

    Drones used to be placed at ``theta = 2*pi*d_id/n``: n evenly-spaced
    *angles*, not n evenly-spaced *points* on the curve. The heart has a cusp
    at ``theta=0``/``2*pi`` (the point at the bottom) where both
    ``dx/dtheta`` and ``dy/dtheta`` go to zero, so arc length per radian
    collapses to almost nothing right there while staying roughly constant
    over the rest of the curve. Equal-angle sampling crowds a disproportionate
    share of drones into that near-zero-length neighbourhood: at N=100 the two
    drones straddling the cusp (ids 49 and 51, symmetric around ``d_id=50`` ->
    ``theta=pi``) landed 0.00 m apart -- not close, coincident. Sampling by
    arc length instead spaces every drone evenly along the curve's actual
    perimeter, which is what "n drones around a shape" should mean regardless
    of how unevenly its parametrization happens to move through theta.
    """
    thetas = [2.0 * math.pi * i / samples for i in range(samples + 1)]
    points = [_heart_xy(t, scale=1.0) for t in thetas]
    cumulative = [0.0]
    for i in range(1, len(points)):
        cumulative.append(cumulative[-1] + math.dist(points[i - 1], points[i]))
    return thetas, cumulative, cumulative[-1]


def _heart_theta_at_arc_fraction(fraction: float) -> float:
    """The theta whose cumulative arc length is ``fraction`` of the way around the curve."""
    thetas, cumulative, total = _heart_arc_length_table()
    target = fraction * total
    idx = bisect.bisect_left(cumulative, target)
    if idx <= 0:
        return thetas[0]
    if idx >= len(cumulative):
        return thetas[-1]
    c0, c1 = cumulative[idx - 1], cumulative[idx]
    t0, t1 = thetas[idx - 1], thetas[idx]
    local = 0.0 if c1 == c0 else (target - c0) / (c1 - c0)
    return t0 + local * (t1 - t0)


def _heart_scale(n: int) -> float:
    """Curve scale, grown with n so the perimeter grows with the fleet it has
    to hold. See _heart_formation."""
    # Arc-length spacing alone (above) fixes the cusp-crowding degeneracy, but
    # the curve's perimeter at the historical fixed 0.25 scale is only ~25 m --
    # far too small to hold 100 drones design_separation_m apart no matter how
    # evenly they're spread along it (avg spacing ~0.26 m). Scale grows with
    # n so the perimeter grows with the fleet it has to hold, floored at the
    # original 0.25 so small fleets -- where the fixed scale was never the
    # problem -- are unaffected.
    return max(0.25, 0.06 * n)


@functools.lru_cache(maxsize=8)
def _heart_theta_assignment(n: int) -> tuple[int, ...]:
    """Which of the n evenly-arc-spaced curve points each drone id flies to.

    Arc-length spacing (``_heart_theta_at_arc_fraction``) makes the n curve
    points themselves well-separated at rest, but ``_run_show`` blends every
    drone in along a straight line from its own launch pad to its formation
    target *as that target continues moving* (the formation keeps rotating
    through the whole choreography, not just after blend completes). Handing
    out those n points in raw ``d_id`` order ignores where each drone's pad
    actually is, so a pad nowhere near a given curve point can still be
    assigned it -- e.g. at n=100, ids 60 apart share an altitude layer (the
    layer cycle) *and* can land on opposite sides of the heart, so their pads
    (a few metres apart) blend toward wildly different targets and their
    straight-line paths cross close to the pads, before altitude or distance
    has grown enough to separate them: measured as low as 0.22 m, well under
    design_separation_m, though the curve's own points are never that close.
    ``optimal_assignment`` -- built for exactly this "naive index-order
    mapping makes drones cross" failure mode -- instead matches each pad to
    the curve point minimizing total travel, which almost never crosses.
    Assumes the default launch grid (``launch_grid``); an explicit
    ``spawn_offsets`` override is a rare, legacy path this does not special-case.
    """
    pads = launch_grid(MAX_FLEET_SIZE)[:n]
    scale = _heart_scale(n)
    curve_points = [
        _heart_xy(_heart_theta_at_arc_fraction(i / n), scale=scale) for i in range(n)
    ]
    from_pts = [{"x": x, "y": y, "z": 0.0} for x, y in pads]
    to_pts = [
        {"x": 5.0 + hx, "y": float(n - 1) + hy, "z": 0.0} for hx, hy in curve_points
    ]
    return tuple(optimal_assignment(from_pts, to_pts))


def _heart_formation(d_id: int, n: int, ratio: float, height: float):
    scale = _heart_scale(n)
    curve_idx = _heart_theta_assignment(n)[d_id]
    theta = _heart_theta_at_arc_fraction(curve_idx / n)
    hx, hy = _heart_xy(theta, scale=scale)
    rot = 2.0 * math.pi * ratio * 0.25  # 90 degree spin
    return (
        5.0 + (hx * math.cos(rot) - hy * math.sin(rot)),
        float(n - 1) + (hx * math.sin(rot) + hy * math.cos(rot)),
        -height,
    )


def _heart_yaw(d_id: int, n: int, ratio: float) -> float:
    """Unwrapped heading, degrees — see _ShapeSpec.yaw on why it is not wrapped."""
    return math.degrees(2.0 * math.pi * ratio * 0.25)


def _compact_lane_grid(n: int) -> list[tuple[float, float]]:
    """A compact (x, y) footprint of side*side*side slots, side = ceil(n**(1/3)).

    Shared by the cube and spline formations so that any two drones assigned
    *different* footprint cells sit >= 2.5 m apart horizontally — comfortably
    clear of ``design_separation_m`` — while the whole footprint stays a few
    metres wide regardless of fleet size (side grows as the cube root of n,
    not sqrt(n)), which is what keeps it inside the geofence at N=100 without
    having to touch the launch-pad grid.

    The list has more entries (``side**3``) than distinct (x, y) pairs
    (``side**2``): built with an extra outer loop so the same footprint cell
    repeats every ``side**2`` entries. A shape indexes into it with
    ``d_id % len(grid)`` and leans on a *third*, per-drone-unique axis to
    separate the drones that land on a repeated cell — see
    ``_cube_formation``/``_spline_formation``, which both use ``height`` (this
    drone's own altitude layer, unchanged since takeoff) for exactly that,
    rather than adding a second, coarser grid axis of their own. That used to
    be `_cube_grid`'s own z step: 1.5 m, independently of `height`, is both
    below `design_separation_m` on its own and, being unrelated to the
    altitude layer already separating drones since takeoff, offered no
    protection at all during the pad<->formation blend or the landing return,
    where two drones assigned to unrelated grid cells fly straight lines that
    can cross — separation there measured as low as 0.2 m. Vertical
    separation that is *never* zero (because it never changes across the
    whole show) removes that crossing hazard everywhere the footprint alone
    cannot, at the modest cost of drones on the same footprint cell being
    stacked by the same layering every other shape already uses instead of by
    a shape-specific z-grid.
    """
    side = max(2, int(math.ceil(n ** (1 / 3.0))))
    return [
        (float(x * 2.5 + 4.0), float(y * 2.5 + 1.0))
        for _z in range(side)
        for y in range(side)
        for x in range(side)
    ]


def _cube_formation(d_id: int, n: int, ratio: float, height: float):
    grid = _compact_lane_grid(n)
    gx, gy = grid[d_id % len(grid)]
    return gx + 1.0 * math.sin(2.0 * math.pi * ratio), gy, -height


def _spline_formation(d_id: int, n: int, ratio: float, height: float):
    """Cubic Bezier sweep, swept out and back over the choreography.

    Control points used to be the same four points for every drone —
    ``d_id``/``n`` were accepted but never read — so at full blend every
    drone converged onto the identical curve; only the altitude layer kept
    them apart, and that layer cycles every ~60 drones (`tara_core.layout`),
    so two drones 60 apart landed on literally the same point at N=100
    (measured separation: 0.0 m). Each drone now sweeps its own curve, offset
    to its own cell of the same compact footprint grid `_cube_formation`
    uses — a rigid per-drone translation, so it changes where the sweep sits
    but not its shape, and any two drones on different cells stay >= 2.5 m
    apart horizontally at every instant of the sweep, not just at rest. The
    sweep amplitude here (2/4/6 m, half the original 4/8/12 m) is deliberately
    modest: the launch-pad grid a drone blends in from already reaches to
    within a few tenths of a metre of the geofence at N=100 (`tara_core.
    layout.launch_grid`'s own near-square-then-capped layout — a separate,
    already-tracked gap, not something this fix widens), so the formation
    itself has very little headroom left to add before tipping a drone
    starting near that edge over 30 m from the origin.
    """
    lane = _compact_lane_grid(n)
    lx, ly = lane[d_id % len(lane)]
    p0 = (lx, ly, -height)
    p1 = (lx + 2.0, ly + 2.0, -height - 2.0)
    p2 = (lx + 4.0, ly - 2.0, -height + 1.5)
    p3 = (lx + 6.0, ly, -height)

    tb = ratio * 2.0 if ratio <= 0.5 else (1.0 - ratio) * 2.0
    mt = 1.0 - tb
    def bez(i):
        return (
            mt ** 3 * p0[i]
            + 3 * mt ** 2 * tb * p1[i]
            + 3 * mt * tb ** 2 * p2[i]
            + tb ** 3 * p3[i]
        )

    return bez(0), bez(1), bez(2)


def _spline_yaw(d_id: int, n: int, ratio: float) -> float:
    """Heading along the sweep, turned rather than flipped at the turnaround.

    This used to be a bare ``90 if ratio <= 0.5 else 270`` — the drone faces
    down-course on the way out and back on the way in, which is the right
    intent, but as a step it put 180 deg of rotation between two adjacent
    samples. That is a yaw-rate violation at any show length, since the step
    size does not shrink as the timeline stretches. The turn is now spread
    over the middle fifth of the sweep, where the drone is at the far end of
    the curve and physically slowest.
    """
    turn_half_width = 0.1
    if ratio <= 0.5 - turn_half_width:
        return 90.0
    if ratio >= 0.5 + turn_half_width:
        return 270.0
    progress = (ratio - (0.5 - turn_half_width)) / (2.0 * turn_half_width)
    return 90.0 + 180.0 * progress


def _zero_yaw(d_id: int, n: int, ratio: float) -> float:
    return 0.0


#: Shape name -> its formation geometry. Keyed the same way the API dispatches.
_SHAPES = {
    "circle": _ShapeSpec(_circle_formation, _circle_yaw, revolutions=1.0),
    "helix": _ShapeSpec(_helix_formation, _helix_yaw, revolutions=1.5),
    "heart": _ShapeSpec(_heart_formation, _heart_yaw, revolutions=0.25),
    "cube": _ShapeSpec(_cube_formation, _zero_yaw, revolutions=0.0),
    # The spline holds a heading out and back, but turns 180 deg at the
    # turnaround, and _spline_yaw compresses that turn into the middle fifth of
    # the sweep — so the yaw budget it needs is that half-turn spread over 1/5
    # of the window, i.e. 2.5 half-turns' worth of window.
    "spline": _ShapeSpec(_spline_formation, _spline_yaw, revolutions=2.5),
}


def generate_circle(num_drones: int, duration: float, rate: float, spawn_offsets: list = None, target_heights: list = None) -> dict:
    return _run_show(_SHAPES["circle"], num_drones, duration, rate, spawn_offsets, target_heights)


def generate_helix(num_drones: int, duration: float, rate: float, spawn_offsets: list = None, target_heights: list = None) -> dict:
    return _run_show(_SHAPES["helix"], num_drones, duration, rate, spawn_offsets, target_heights)


def generate_heart(num_drones: int, duration: float, rate: float, spawn_offsets: list = None, target_heights: list = None) -> dict:
    return _run_show(_SHAPES["heart"], num_drones, duration, rate, spawn_offsets, target_heights)


def generate_cube(num_drones: int, duration: float, rate: float, spawn_offsets: list = None, target_heights: list = None) -> dict:
    return _run_show(_SHAPES["cube"], num_drones, duration, rate, spawn_offsets, target_heights)


def generate_spline(num_drones: int, duration: float, rate: float, spawn_offsets: list = None, target_heights: list = None) -> dict:
    return _run_show(_SHAPES["spline"], num_drones, duration, rate, spawn_offsets, target_heights)
