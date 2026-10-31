"""
Taramandal Studio - Trajectory Generation Library
"""

import bisect
import dataclasses
import functools
import math

import numpy as np

from tara_core import DEFAULT_LIMITS, MAX_FLEET_SIZE, altitude_layers, launch_grid
from tara_core.assignment import optimal_assignment
from tara_core.geometry import quintic_profile

from .pacing import MAX_ORBIT_SPEED_MS, ShowPacing, formation_spread, plan_pacing
from .voxelizer import _contour_length, _sample_contours

#: Where circle, helix and heart formations are centred: the fleet origin, the
#: point the GCS geofence is measured from. They rotate about this centre, so
#: a formation that fits inside the fence stays inside it at every angle, and
#: the straight blend/landing legs between the pads and the formation stay
#: inside it too (both ends lie in the same convex disc).
FORMATION_CENTER = (0.0, 0.0)

#: Farthest a flat outline formation may reach from FORMATION_CENTER: the
#: geofence radius less a 2 m margin for waypoint rounding and position noise.
FORMATION_MAX_RADIUS = DEFAULT_LIMITS.geofence_radius_m - 2.0

#: Horizontal spacing kept between neighbouring drones on an outline, and
#: between nested outlines. The same 1.2x margin over design_separation_m the
#: single-ring radius has always used, so a same-layer pair is never relying
#: on the altitude stagger for its clearance.
OUTLINE_SPACING = 1.2 * DEFAULT_LIMITS.design_separation_m

#: Room left under the altitude ceiling for shapes that move drones above their
#: own hover layer. The spline's highest control point sits 2 m above the
#: layer (and a Bezier curve never leaves its control points' hull); the
#: helix's bob is 0.4 m. Without it, the top layer at N>=60 sits exactly on
#: the 120 m ceiling and both shapes cross it.
FORMATION_HEADROOM_M = 2.0

_LAYER_LIMITS = dataclasses.replace(
    DEFAULT_LIMITS, max_altitude_m=DEFAULT_LIMITS.max_altitude_m - FORMATION_HEADROOM_M
)



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
        target_heights = altitude_layers(num_drones, limits=_LAYER_LIMITS)
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
    """Minimum-jerk quintic: a 0->1 ramp with zero velocity AND acceleration
    at both ends.

    Phase transitions used to be linear ramps, which meant the drone's velocity
    stepped discontinuously at every phase boundary — from a standstill to full
    blend speed between two samples. Velocity/dt is acceleration, so those steps
    read as acceleration spikes no matter how long the phases are, which is
    exactly the kind of violation extra show time cannot fix. Smoothstep
    (``r^2 * (3 - 2r)``, the original fix here) zeros velocity at both ends,
    which was enough to pass the acceleration check, but its own second
    derivative is nonzero at ``r=0``/``r=1`` — acceleration still jumps the
    instant a phase starts or ends, i.e. an infinite/discontinuous jerk spike
    at every phase boundary (issue #60). ``tara_core.geometry.quintic_profile``
    is the minimum-jerk polynomial that also zeros acceleration at both ends,
    so acceleration — not just velocity — is now continuous across every
    takeoff/blend/land phase boundary.
    """
    position, _velocity, _acceleration, _jerk = quintic_profile(ratio)
    return position


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
    orbit_reach_m = spec.orbit_reach_fn(num_drones) if spec.orbit_reach_fn else 0.0
    return plan_pacing(
        num_drones,
        climb_m=climb_m,
        spread_m=spread_m,
        revolutions=spec.revolutions,
        orbit_reach_m=orbit_reach_m,
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
        orbit_reach_fn: ``(n) -> metres``, the radius this shape's formation
            actually translates around while it turns (issue #88) — ``None``
            for every shape that only spins in place or holds a heading
            (currently every shape but circle).
    """

    def __init__(self, formation, yaw, revolutions: float, orbit_reach_fn=None):
        self.formation = formation
        self.yaw = yaw
        self.revolutions = revolutions
        self.orbit_reach_fn = orbit_reach_fn


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


def _min_gap(a: np.ndarray, b: np.ndarray) -> float:
    """Smallest distance between any vertex of polyline ``a`` and any of ``b``."""
    return float(np.sqrt(((a[:, None, :] - b[None, :, :]) ** 2).sum(axis=2)).min())


def _nested_outlines(outline, reach: float, natural_scale: float, n: int) -> list[np.ndarray]:
    """Closed outlines that hold ``n`` drones OUTLINE_SPACING apart within
    FORMATION_MAX_RADIUS of the formation centre.

    A single outline has a hard capacity: 100 drones on one ring at the
    spacing the ring needs is a 38 m radius, past the 30 m geofence no matter
    where it is centred. When the shape's natural size fits, it is used
    unchanged. Otherwise the outermost copy is scaled to touch
    FORMATION_MAX_RADIUS and smaller copies are nested inside it, each scaled
    down just far enough to sit OUTLINE_SPACING clear of the one outside it,
    until their combined length can hold the fleet at that spacing (with 10%
    for rounding when drones are shared out between outlines).

    Args:
        outline: ``scale -> (M, 2)`` dense closed polyline about the origin.
        reach: Farthest point of ``outline(1.0)`` from the origin.
        natural_scale: The scale the shape would use if nothing constrained it.
        n: Fleet size.
    """
    if natural_scale * reach <= FORMATION_MAX_RADIUS:
        return [outline(natural_scale)]

    scale = FORMATION_MAX_RADIUS / reach
    outlines = [outline(scale)]
    length = _contour_length(outlines[0])
    while length < 1.1 * n * OUTLINE_SPACING:
        # The gap to the enclosing outline grows as the copy shrinks, so the
        # largest scale that still clears it is found by bisection.
        lo, hi = 0.0, scale
        for _ in range(40):
            mid = (lo + hi) / 2.0
            if _min_gap(outlines[-1], outline(mid)) >= OUTLINE_SPACING:
                lo = mid
            else:
                hi = mid
        if lo * reach < OUTLINE_SPACING:
            break  # no room left for another outline; the validator reports the shortfall
        scale = lo
        outlines.append(outline(scale))
        length += _contour_length(outlines[-1])
    return outlines


def _ring(scale: float, samples: int = 720) -> np.ndarray:
    theta = np.linspace(0.0, 2.0 * np.pi, samples, endpoint=False)
    return np.column_stack([scale * np.cos(theta), scale * np.sin(theta)])


def _heart_outline(scale: float, samples: int = 720) -> np.ndarray:
    return np.array([
        _heart_xy(_heart_theta_at_arc_fraction(i / samples), scale=scale) for i in range(samples)
    ])


@functools.lru_cache(maxsize=16)
def _outline_slots(shape: str, n: int) -> tuple[tuple[float, float], ...]:
    """Each drone's (x, y) in a flat outline formation, relative to its centre.

    Points are spread over the outlines by length with the voxelizer's own
    contour sampler, then handed to drones with ``optimal_assignment`` from
    their launch pads: the minimum-travel matching that keeps the straight
    pad-to-formation legs from crossing (see _heart_formation for the failure
    it replaced). Assumes the default launch grid; an explicit
    ``spawn_offsets`` override is a rare, legacy path this does not special-case.
    """
    if shape == "circle":
        outlines = _nested_outlines(_ring, 1.0, _circle_radius(n), n)
    else:
        outlines = _nested_outlines(_heart_outline, _HEART_REACH, _heart_scale(n), n)
    points = _sample_contours(outlines, n)
    pads = launch_grid(MAX_FLEET_SIZE)[:n]
    cx, cy = FORMATION_CENTER
    perm = optimal_assignment(
        [{"x": x, "y": y, "z": 0.0} for x, y in pads],
        [{"x": cx + px, "y": cy + py, "z": 0.0} for px, py in points],
    )
    return tuple((float(points[k][0]), float(points[k][1])) for k in perm)


def _rotated_slot(shape: str, d_id: int, n: int, angle: float) -> tuple[float, float]:
    sx, sy = _outline_slots(shape, n)[d_id]
    c, s = math.cos(angle), math.sin(angle)
    return FORMATION_CENTER[0] + sx * c - sy * s, FORMATION_CENTER[1] + sx * s + sy * c


@functools.lru_cache(maxsize=16)
def _circle_reach_m(n: int) -> float:
    """Farthest any drone's circle slot sits from FORMATION_CENTER, metres.

    Shared by ``_circle_sweep`` (the sweep clamp) and ``_pacing_for`` (the
    pacing floor, issue #88) so the two stay measuring the same ring.
    """
    slots = _outline_slots("circle", n)
    return max(math.hypot(x, y) for x, y in slots)


@functools.lru_cache(maxsize=16)
def _circle_sweep(n: int) -> float:
    """How far the ring turns over the choreography, radians.

    ``plan_pacing``'s ``orbit_reach_m`` (issue #88) already sizes the window
    for a full turn whenever one is physically feasible, so this is normally
    a no-op (orbit * choreo_s / reach == 2*pi, by construction — both this
    and plan_pacing's own ``orbit_s`` are the same distance-over-speed
    budget). It still legitimately clamps below a full turn in the one case
    plan_pacing's floor can't fix: the blend's own eased peak alone already
    consuming the 90%-of-ceiling budget, leaving no speed for the orbit at
    all (the ``orbit <= 0`` branch below).
    """
    reach = _circle_reach_m(n)
    if reach <= 0:
        return 2.0 * math.pi
    # Same measurement _pacing_for makes, taken straight from the slots (the
    # ring at ratio 0) so this does not recurse through _circle_formation.
    start_positions, heights = _launch_state(n, None, None)
    hover = {d: {"x": p["x"], "y": p["y"], "z": -heights[d]} for d, p in start_positions.items()}
    slots = _outline_slots("circle", n)
    entry = {
        d: (FORMATION_CENTER[0] + sx, FORMATION_CENTER[1] + sy, -heights[d])
        for d, (sx, sy) in enumerate(slots)
    }
    spread_m = formation_spread(hover, entry)
    pacing = plan_pacing(
        n, climb_m=max(heights.values()), spread_m=spread_m, revolutions=1.0, orbit_reach_m=reach
    )
    blend_peak = 1.5 * spread_m / pacing.blend_s
    orbit = min(MAX_ORBIT_SPEED_MS, 0.9 * DEFAULT_LIMITS.max_velocity_ms - blend_peak)
    return max(0.0, min(2.0 * math.pi, orbit * pacing.choreo_s / reach))


def _circle_formation(d_id: int, n: int, ratio: float, height: float):
    x, y = _rotated_slot("circle", d_id, n, _circle_sweep(n) * ratio)
    return x, y, -height


def _circle_yaw(d_id: int, n: int, ratio: float) -> float:
    """Unwrapped heading, degrees — see _ShapeSpec.yaw on why it is not wrapped."""
    sx, sy = _outline_slots("circle", n)[d_id]
    return math.degrees(math.atan2(sy, sx) + _circle_sweep(n) * ratio)


@functools.lru_cache(maxsize=16)
def _helix_angles(n: int) -> tuple[tuple[float, ...], float]:
    """Each drone's starting angle on the helix ring, and the turns it makes.

    Up to one drone per hover layer the helix is unchanged: drone i at
    ``2*pi*i/n``, one and a half turns. Past that, drones share layers
    (``altitude_layers`` cycles), and a shared layer means only horizontal
    distance keeps the pair apart. With the old ``2*pi*i/n`` spacing the
    offset between two such drones pointed a different way for every pair
    while their pads differed in roughly the same direction, so about half
    the pairs crossed on their landing legs (measured 0.69 m at N=100).

    So the helix becomes a double helix: the drones on a layer sit on opposite
    sides of the ring, and each pair is placed so its ring offset points the
    same way as its pad offset. Their straight legs between pad and ring then
    run side by side instead of crossing. The helix also makes a whole turn
    instead of one and a half, so the landing legs start from the same ring
    positions the blend ended on rather than the opposite side.
    Assumes the default launch grid and hover layers.
    """
    layers = altitude_layers(n, limits=_LAYER_LIMITS)
    distinct = sorted(set(layers))
    if len(distinct) == n:
        return tuple(2.0 * math.pi * i / n for i in range(n)), 1.5

    pads = launch_grid(MAX_FLEET_SIZE)[:n]
    level = {h: k for k, h in enumerate(distinct)}
    groups: dict[int, list[int]] = {}
    for d_id, h in enumerate(layers):
        groups.setdefault(level[h], []).append(d_id)

    # A single dot-product ("does this pair's ring offset point the same way
    # as its pad offset") only checks the shapes' *resting* positions. It
    # cannot see the real failure mode: the ring keeps rotating throughout
    # the blend (see _helix_angle), so what actually determines whether a
    # pair's straight blend legs cross is the closest approach of two moving
    # targets, not two fixed points -- confirmed by direct comparison: the
    # dot product disagrees with a real simulation on 25 of this fleet
    # size's 41 paired groups (checked at n=100), including the ones that
    # cost drones 12/71 their separation (#72 x #73 interaction).
    #
    # So this now runs an actual simulation per pair, self-adapting to
    # whatever the current entry-blend easing is rather than assuming any
    # particular curve's shape (does not go stale the next time it changes,
    # unlike the dot product it replaces). It needs this shape's blend/choreo
    # timing to do that, which in turn needs *an* assignment of these pairs
    # to compute (pacing is sized off the fleet's worst-case pad-to-target
    # spread) -- so a first pass with the original dot-product heuristic
    # bootstraps that timing (accurate enough: which of two symmetric ring
    # points a pair aims for barely moves the fleet-wide worst case), and a
    # second pass re-decides each pair against the real timing it produces.
    bootstrap = [0.0] * n
    for k, members in groups.items():
        base = 2.0 * math.pi * k / len(distinct)
        if len(members) == 2:
            i, j = members
            ring = (math.cos(base) - math.cos(base + math.pi), math.sin(base) - math.sin(base + math.pi))
            pad = (pads[i][0] - pads[j][0], pads[i][1] - pads[j][1])
            if ring[0] * pad[0] + ring[1] * pad[1] < 0:
                i, j = j, i
            bootstrap[i], bootstrap[j] = base, base + math.pi
        else:
            for m, d_id in enumerate(members):
                bootstrap[d_id] = base + 2.0 * math.pi * m / len(members)

    start_positions, heights = _launch_state(n, None, None)
    entry_targets = {
        d_id: (5.0 * math.cos(bootstrap[d_id]), 5.0 * math.sin(bootstrap[d_id]), -heights[d_id])
        for d_id in range(n)
    }
    hover_positions = {
        d_id: {"x": p["x"], "y": p["y"], "z": -heights[d_id]} for d_id, p in start_positions.items()
    }
    spread_m = formation_spread(hover_positions, entry_targets)
    pacing = plan_pacing(n, climb_m=max(heights.values()), spread_m=spread_m, revolutions=1.0)
    blend_frac = pacing.blend_s / pacing.choreo_s if pacing.choreo_s > 0 else 0.0

    def min_blend_separation(pad_a, angle_a, pad_b, angle_b, samples: int = 60) -> float:
        """Closest approach during the entry blend (pad -> rotating ring
        point). Sampled directly against blend_ratio -- see the docstring
        above for why real elapsed/blend_s time is not needed."""
        worst = math.inf
        for step in range(samples + 1):
            b = step / samples
            eff = _ease(b) * blend_frac
            ax, ay = 5.0 * math.cos(angle_a + 2.0 * math.pi * eff), 5.0 * math.sin(angle_a + 2.0 * math.pi * eff)
            bx, by = 5.0 * math.cos(angle_b + 2.0 * math.pi * eff), 5.0 * math.sin(angle_b + 2.0 * math.pi * eff)
            xa = pad_a[0] + b * (ax - pad_a[0])
            ya = pad_a[1] + b * (ay - pad_a[1])
            xb = pad_b[0] + b * (bx - pad_b[0])
            yb = pad_b[1] + b * (by - pad_b[1])
            worst = min(worst, math.hypot(xa - xb, ya - yb))
        return worst

    def min_return_separation(pad_a, angle_a, pad_b, angle_b, samples: int = 60) -> float:
        """Closest approach during the landing return leg: a full turn
        (turns=1.0) brings each drone's land_entry back to exactly the ring
        point it entered on, then _land_waypoint interpolates ring point ->
        pad on fixed endpoints. Both endpoints are fixed on each side, so
        (unlike the blend leg) this is invariant to which easing curve is
        used -- checked here as plain linear interpolation weight, not
        because the real return leg is linear, but because the *minimum*
        distance between two such interpolations does not depend on the
        monotonic curve reparametrizing how that weight is reached over time.
        """
        worst = math.inf
        for step in range(samples + 1):
            w = step / samples
            xa = 5.0 * math.cos(angle_a) + w * (pad_a[0] - 5.0 * math.cos(angle_a))
            ya = 5.0 * math.sin(angle_a) + w * (pad_a[1] - 5.0 * math.sin(angle_a))
            xb = 5.0 * math.cos(angle_b) + w * (pad_b[0] - 5.0 * math.cos(angle_b))
            yb = 5.0 * math.sin(angle_b) + w * (pad_b[1] - 5.0 * math.sin(angle_b))
            worst = min(worst, math.hypot(xa - xb, ya - yb))
        return worst

    angles = [0.0] * n
    for k, members in groups.items():
        base = 2.0 * math.pi * k / len(distinct)
        if len(members) == 2:
            i, j = members
            # Whichever assignment gives the pair's *worse* leg (blend or
            # landing) the larger margin -- optimizing blend alone can (and,
            # checked at n=100, does for one pair) pick the orientation
            # that's worse for the landing return, since the two legs are
            # not mirror images of the same geometry once one moves and the
            # other doesn't.
            def worst_leg(first, second):
                return min(
                    min_blend_separation(pads[first], base, pads[second], base + math.pi),
                    min_return_separation(pads[first], base, pads[second], base + math.pi),
                )
            if worst_leg(i, j) < worst_leg(j, i):
                i, j = j, i
            angles[i], angles[j] = base, base + math.pi
        else:
            for m, d_id in enumerate(members):
                angles[d_id] = base + 2.0 * math.pi * m / len(members)
    return tuple(angles), 1.0


@functools.lru_cache(maxsize=16)
def _helix_pacing(n: int) -> ShowPacing:
    """This shape's own blend/choreo timing, used by ``_helix_angle`` to keep
    a paired drone's ring-target rotation synced to its own blend progress.

    Safe against the recursion this looks like it should cause: computing
    pacing calls ``_pacing_for``, which evaluates ``_helix_formation`` at
    ``ratio=0.0`` to find each drone's entry point -- which calls
    ``_helix_angle`` again. ``_helix_angle`` short-circuits ``ratio <= 0.0``
    before it ever reaches this cache, so that inner call never re-enters
    here; this cache is only populated once ``_run_show`` starts asking for
    ``ratio > 0``, by which point the shape's pacing is a well-defined,
    already-computable quantity.
    """
    start_positions, heights = _launch_state(n, None, None)
    return _pacing_for(_SHAPES["helix"], n, start_positions, heights)


def _helix_angle(d_id: int, n: int, ratio: float) -> float:
    """Each drone's ring angle at a given point in the choreography.

    Two same-layer drones are placed diametrically opposite (see
    ``_helix_angles``) on the premise that their straight blend/landing legs
    then run side by side rather than crossing -- true only if both drones'
    *ring target* advances in step with their *own progress into the blend*.
    Post-#60 (jerk-continuous quintic easing replacing smoothstep, PR #72)
    it no longer does: the ring's rotation here was driven by raw elapsed
    time while the pad->target blend weight (``_run_show``'s
    ``blend_ratio``) was eased, so a change in the easing curve's *shape*
    (not just its endpoints) shifts how far the ring has turned by the time
    a given blend fraction is reached -- confirmed to bring a paired pair
    (drones 12 & 71 at n=100) to 1.35m, under the 2.0m design separation,
    during the blend window (measured at blend_ratio=0.48, not landing).
    Re-derived rather than re-tuned: during blend, the ring's own progress
    now tracks the *actual* current ``_ease`` curve (whatever shape that is)
    scaled into the same [0, ratio-at-blend-end] range raw time would have
    given it, rather than assuming any particular curve's peak/mean shape --
    so this does not go stale again the next time the easing changes. Once
    blend completes the ring reverts to its original, real-time rotation.
    """
    starts, turns = _helix_angles(n)
    if ratio <= 0.0:
        return starts[d_id]
    pacing = _helix_pacing(n)
    elapsed = ratio * pacing.choreo_s
    if pacing.blend_s > 0.0 and elapsed < pacing.blend_s:
        effective_ratio = _ease(elapsed / pacing.blend_s) * (pacing.blend_s / pacing.choreo_s)
    else:
        effective_ratio = ratio
    return starts[d_id] + 2.0 * math.pi * effective_ratio * turns


def _helix_formation(d_id: int, n: int, ratio: float, height: float):
    angle = _helix_angle(d_id, n, ratio)
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
        FORMATION_CENTER[0] + 5.0 * math.cos(angle),
        FORMATION_CENTER[1] + 5.0 * math.sin(angle),
        -max(1.5, height + height_osc),
    )


def _helix_yaw(d_id: int, n: int, ratio: float) -> float:
    """Unwrapped heading, degrees — see _ShapeSpec.yaw on why it is not wrapped."""
    return math.degrees(_helix_angle(d_id, n, ratio))


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


#: Farthest point of the unit heart (``_heart_xy`` at scale 1) from its origin,
#: which is the point the heart spins about.
_HEART_REACH = max(
    math.hypot(*_heart_xy(2.0 * math.pi * i / 4000, scale=1.0)) for i in range(4000)
)


def _heart_formation(d_id: int, n: int, ratio: float, height: float):
    # Why each drone's point on the curve comes from an optimal assignment
    # rather than d_id order: at n=100, ids a layer-cycle apart share an
    # altitude and, handed out in index order, could land on opposite sides of
    # the heart with pads a few metres apart, so their straight blend legs
    # crossed near the pads (measured 0.22 m). Matching pads to curve points by
    # minimum travel (see _outline_slots) almost never crosses.
    x, y = _rotated_slot("heart", d_id, n, 2.0 * math.pi * ratio * 0.25)  # 90 degree spin
    return x, y, -height


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


@functools.lru_cache(maxsize=16)
def _lane_assignment(n: int, formation_at_cell, revolutions: float) -> tuple:
    """Cell index into ``_compact_lane_grid(n)`` for each drone id, 0..n-1.

    The base assignment (``d_id % len(grid)``) has no reason to avoid a
    same-altitude-layer pair's straight pad<->cell legs crossing during
    blend or choreo: ``_compact_lane_grid``'s own guarantee (different
    cells stay >= 2.5 m apart) is about the formation sweep, not that leg,
    and two drones sharing a layer hold the *identical* height the whole
    time (see ``_compact_lane_grid``'s docstring) -- there is no vertical
    margin to fall back on either. #167 review: widening the launch-pad
    grid by just 2% flipped several same-layer pairs at N=100 from a 2.0 m
    clearance to 0.97 m -- the margin at the old, exact 2.0 m spacing was a
    coincidence of that one number, not a guarantee that survives any
    pad-grid change. Mirrors ``_helix_angles``' fix for the identical
    problem on the ring: simulate both orderings of a same-layer pair's
    cell assignment against the shape's real motion and keep whichever
    gives the larger worst-case separation. Landing is not re-checked here:
    its own sequencing (see ``_land_waypoint``) already returns to the
    already-safe pad grid before giving up altitude, independently of
    which cell a drone came from.

    Args:
        n: Fleet size.
        formation_at_cell: ``(cell, ratio, height) -> (x, y, z)``, the
            shape's geometry given a lane cell directly (no ``d_id`` lookup).
        revolutions: This shape's heading-turn count, passed straight to
            ``plan_pacing`` the same way ``_pacing_for`` does.
    """
    pads = launch_grid(MAX_FLEET_SIZE)[:n]
    lane = _compact_lane_grid(n)
    heights = altitude_layers(n, limits=_LAYER_LIMITS)
    base = [d_id % len(lane) for d_id in range(n)]

    distinct = sorted(set(heights))
    level = {h: k for k, h in enumerate(distinct)}
    groups: dict[int, list[int]] = {}
    for d_id, h in enumerate(heights):
        groups.setdefault(level[h], []).append(d_id)

    def formation(d_id, num_drones, ratio, height):
        return formation_at_cell(lane[base[d_id]], ratio, height)

    start_positions = {d_id: {"x": pads[d_id][0], "y": pads[d_id][1], "z": 0.0} for d_id in range(n)}
    target_heights = {d_id: heights[d_id] for d_id in range(n)}
    pacing = _pacing_for(_ShapeSpec(formation, _zero_yaw, revolutions), n, start_positions, target_heights)

    def worst_separation(i, j, cell_i, cell_j, samples: int = 60) -> float:
        """Closest approach across the whole blend+choreo window (ratio 0 to
        1), replicating _run_show's blend formula exactly for these two
        drones under a hypothetical (cell_i, cell_j) assignment."""
        worst = math.inf
        for step in range(samples + 1):
            ratio = step / samples
            elapsed = ratio * pacing.choreo_s
            blend_ratio = _ease(elapsed / pacing.blend_s) if pacing.blend_s > 0 else 1.0
            xi, yi, zi = formation_at_cell(cell_i, ratio, heights[i])
            xj, yj, zj = formation_at_cell(cell_j, ratio, heights[j])
            xi = pads[i][0] + blend_ratio * (xi - pads[i][0])
            yi = pads[i][1] + blend_ratio * (yi - pads[i][1])
            zi = -heights[i] + blend_ratio * (zi + heights[i])
            xj = pads[j][0] + blend_ratio * (xj - pads[j][0])
            yj = pads[j][1] + blend_ratio * (yj - pads[j][1])
            zj = -heights[j] + blend_ratio * (zj + heights[j])
            worst = min(worst, math.hypot(xi - xj, yi - yj, zi - zj))
        return worst

    assignment = list(base)
    for members in groups.values():
        if len(members) != 2:
            continue
        i, j = members
        cell_i, cell_j = lane[base[i]], lane[base[j]]
        straight = worst_separation(i, j, cell_i, cell_j)
        swapped = worst_separation(i, j, cell_j, cell_i)
        if swapped > straight:
            assignment[i], assignment[j] = base[j], base[i]
    return tuple(assignment)


def _cube_formation_at_cell(cell: tuple[float, float], ratio: float, height: float):
    gx, gy = cell
    return gx + 1.0 * math.sin(2.0 * math.pi * ratio), gy, -height


def _cube_formation(d_id: int, n: int, ratio: float, height: float):
    lane = _compact_lane_grid(n)
    cell = lane[_lane_assignment(n, _cube_formation_at_cell, 0.0)[d_id]]
    return _cube_formation_at_cell(cell, ratio, height)


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

    Which cell a drone sweeps from comes from ``_lane_assignment`` (#167
    review), not a bare ``d_id % len(lane)`` — see its docstring for why: a
    same-altitude-layer pair's straight pad<->cell legs during blend are not
    covered by the >= 2.5 m different-cell guarantee above, which is about
    the sweep itself, not that leg.
    """
    lane = _compact_lane_grid(n)
    cell = lane[_lane_assignment(n, _spline_formation_at_cell, 2.5)[d_id]]
    return _spline_formation_at_cell(cell, ratio, height)


def _spline_formation_at_cell(cell: tuple[float, float], ratio: float, height: float):
    lx, ly = cell
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
    "circle": _ShapeSpec(_circle_formation, _circle_yaw, revolutions=1.0, orbit_reach_fn=_circle_reach_m),
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
