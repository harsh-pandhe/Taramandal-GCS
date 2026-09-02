"""Point-cloud voxelizer: arbitrary geometry to N drone waypoints.

The parametric generators in :mod:`tara_studio.generator` cover six fixed
shapes. Real choreography needs arbitrary geometry - a number ("5"), a word
("INDIA"), a company logo as an SVG path, a CAD model as .obj/.stl - resampled
so that exactly ``N`` drones sit evenly across it.

Three sources, one contract
---------------------------

Each ``sample_*_point_cloud`` function returns exactly ``drone_count`` points,
in a *shape-local* frame: XY, Y-up, centred on the origin and scaled so the
larger extent spans ``scale_meters``. :func:`voxelize` then places that cloud
into the world with :func:`project_to_plane` and reports its bounds. Keeping
sampling and placement separate is what lets one plane/altitude/scale
implementation serve all three sources.

Even spacing is the whole point
-------------------------------

Naive sampling clusters. Evaluating a glyph outline at equal *parameter* steps
bunches points wherever the curve is dense, and sampling mesh faces uniformly
at random leaves both clumps and holes - either way some drones end up metres
apart and others inside each other's rotor wash.

So each sampler resamples by *measured* geometry rather than by parameter:

* **Text and SVG** are 1D contours, so points are placed at equal **arc
  length** along the outline (:func:`_resample_closed_contour`). Contours and
  sub-paths share the budget in proportion to their length, which keeps
  density uniform across a glyph's outer and inner loops alike.
* **A mesh** is a 2D surface, where "equidistant" has no 1D ordering to walk.
  It instead gets **Poisson-disk** sampling (:func:`_poisson_disk_filter`):
  draw a dense candidate pool, then greedily accept points that clear a
  minimum radius, which enforces the inter-point distance constraint directly.

Exactly N, always
-----------------

``len(coordinates) == drone_count`` is a hard postcondition: a show is
choreographed for a specific fleet, and N-1 points means one drone has no
waypoint. This is the one place Poisson-disk needs care, because radius
elimination cannot promise a count - ``trimesh.sample.sample_surface_even``
returns 8 points when asked for 10. :func:`_poisson_disk_filter` therefore
relaxes the radius and retries, then tops up from the leftover candidates, so
the count is met even when the requested spacing is geometrically impossible
on a small mesh.

Scale follows from capacity
---------------------------

Holding both the count *and* the spacing means the formation size cannot also
be fixed. An outline of perimeter ``L`` holds ``L / min_separation_m`` drones
and no more, so 1000 drones at 0.8 m need ~800 m of outline - roughly a 170 m
tall "5", not the requested 20 m one. :func:`contour_feasible_scale` computes
that requirement up front and :func:`voxelize` grows the formation to meet it,
reporting the scale it used. The alternative is worse than it looks: forcing
1000 points onto a 20 m glyph and relaxing the collisions away inflates the
shape to ~30 m regardless, only unevenly and without telling anyone.

Coordinates are NED metres to match the rest of the platform: ``z`` is
negative upwards, so an altitude of 15 m is ``z = -15.0``.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass

import numpy as np

#: Minimum spacing between any two drones in a generated cloud, metres.
#:
#: This is the issue's stated acceptance threshold and the floor this module
#: enforces by default. Note it is *looser* than the platform's real limits in
#: :class:`tara_core.safety.SafetyLimits` (``min_separation_m`` 1.5,
#: ``design_separation_m`` 2.0) - 0.8 m is the value ``tara_core.safety``'s own
#: docstring records as the outdated Studio threshold that predates those
#: limits. A cloud that clears 0.8 m is therefore *not* automatically a flyable
#: formation; callers building a real show should pass
#: ``min_separation_m=DEFAULT_LIMITS.design_separation_m``, and :func:`voxelize`
#: reports the separation it actually achieved so the caller can check.
DEFAULT_MIN_SEPARATION_M = 0.8

#: Candidate pool multiplier for Poisson-disk sampling. The filter needs many
#: more candidates than it accepts, since most are rejected for falling inside
#: an accepted point's radius; 12x keeps a 1000-point cloud tractable while
#: leaving enough leftovers to top up an exact count.
POISSON_OVERSAMPLE = 12

#: Ceiling on the candidate pool, so a large ``drone_count`` cannot make the
#: dart-throwing loop explode.
POISSON_MAX_CANDIDATES = 60_000

#: Font used when a request does not name one. DejaVu Sans ships with
#: matplotlib itself, so text sampling never depends on system fonts.
DEFAULT_FONT_FAMILY = "DejaVu Sans"

#: Nominal point size for glyph outline extraction. The cloud is rescaled to
#: ``scale_meters`` afterwards, so this only sets the precision of the outline
#: matplotlib hands back, not the output size.
GLYPH_FONT_SIZE = 100.0

#: Supported projection planes.
PLANE_XY = "XY"
PLANE_XZ = "XZ"

#: Lowest altitude any generated drone may occupy, metres above ground.
#: ``altitude_meters`` centres the formation, so a tall figure on a low centre
#: would otherwise place its bottom rows underground - a 20 m "5" centred at
#: 5 m reaches to -5 m. :func:`voxelize` lifts the whole cloud to clear this.
#: Matches ``tara_core.layout.BASE_HOVER_ALT_M``, the lowest layer the launch
#: geometry already treats as a safe hover altitude.
MIN_GROUND_CLEARANCE_M = 2.0

#: Relative overshoot applied to the separation target. See
#: :func:`enforce_min_separation` - relaxation converges onto its target, so
#: aiming exactly at the limit lands a few times 1e-5 under it.
SEPARATION_EPS = 1e-3

#: Headroom applied to an auto-computed scale. Arc-length spacing is uniform
#: along a contour but neighbouring contours still pass close to each other, so
#: the theoretical minimum scale is always slightly too tight in practice; 15%
#: leaves the relaxation pass something to work with instead of everything.
SCALE_FEASIBILITY_MARGIN = 1.15


@dataclass(frozen=True)
class BoundingBox:
    """Axis-aligned bounds of a placed point cloud, NED metres."""

    min_x: float
    max_x: float
    min_y: float
    max_y: float
    min_z: float
    max_z: float

    def as_dict(self) -> dict:
        return {
            "min_x": round(self.min_x, 4),
            "max_x": round(self.max_x, 4),
            "min_y": round(self.min_y, 4),
            "max_y": round(self.max_y, 4),
            "min_z": round(self.min_z, 4),
            "max_z": round(self.max_z, 4),
        }


def _validate_count(drone_count: int) -> int:
    if isinstance(drone_count, bool) or not isinstance(drone_count, (int, np.integer)):
        raise ValueError(f"drone_count must be an integer, got {drone_count!r}")
    if drone_count < 1:
        raise ValueError(f"drone_count must be >= 1, got {drone_count}")
    return int(drone_count)


def _normalize_2d(points: np.ndarray, scale_meters: float) -> np.ndarray:
    """Centre a cloud on the origin and scale its largest extent to ``scale_meters``.

    Uniform on every axis - the aspect ratio of a glyph or logo is part of its
    identity, so "5" must not be stretched to fill a square. A degenerate
    extent (a single point, or a perfectly axis-aligned line) would divide by
    zero, so it is left unscaled at the origin instead.
    """
    if scale_meters <= 0:
        raise ValueError(f"scale_meters must be positive, got {scale_meters}")

    mins = points.min(axis=0)
    maxs = points.max(axis=0)
    largest = float((maxs - mins).max())

    centred = points - (mins + maxs) / 2.0
    if largest <= 1e-12:
        return centred
    return centred * (scale_meters / largest)


def _resample_closed_contour(contour: np.ndarray, n: int) -> np.ndarray:
    """Place ``n`` points at equal arc length around one closed contour.

    ``contour`` is the polyline the font/SVG backend produced, whose vertices
    are spaced by curve parameter, not by distance. Walking its cumulative
    chord length and interpolating at equal steps decouples the output spacing
    from that input density - which is what stops points bunching on the tight
    curves of a glyph.

    The contour is treated as closed (the last vertex joins the first) and the
    ``n`` samples are spread over ``[0, total_length)`` so the seam is not
    sampled twice.
    """
    if n <= 0:
        return np.empty((0, 2), dtype=float)

    pts = np.asarray(contour, dtype=float)
    if len(pts) < 2:
        return np.repeat(pts[:1], n, axis=0)

    # Close the loop so the final segment back to the start carries points too.
    if not np.allclose(pts[0], pts[-1]):
        pts = np.vstack([pts, pts[:1]])

    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    total = float(cum[-1])
    if total <= 1e-12:
        return np.repeat(pts[:1], n, axis=0)

    targets = np.linspace(0.0, total, n, endpoint=False)
    x = np.interp(targets, cum, pts[:, 0])
    y = np.interp(targets, cum, pts[:, 1])
    return np.column_stack([x, y])


def _allocate_by_length(lengths: list[float], n: int) -> list[int]:
    """Split ``n`` points across parts in proportion to their length.

    Largest-remainder apportionment: floor each share, then hand the leftover
    points to the parts with the largest fractional remainder. That keeps the
    total exactly ``n`` (plain rounding does not) while keeping point *density*
    equal across parts, so the dot of an "i" does not get the same budget as
    the stroke of an "I".

    Every part with non-zero length is reserved at least one point when ``n``
    allows it, so no contour of the shape silently disappears.
    """
    parts = len(lengths)
    if parts == 0:
        return []
    total = float(sum(lengths))
    if total <= 1e-12:
        # No measurable length anywhere: split as evenly as possible.
        base, rem = divmod(n, parts)
        return [base + (1 if i < rem else 0) for i in range(parts)]

    # Reserve one point per non-empty part, so nothing vanishes.
    nonempty = [i for i, ln in enumerate(lengths) if ln > 1e-12]
    counts = [0] * parts
    reserved = 0
    if n >= len(nonempty):
        for i in nonempty:
            counts[i] = 1
        reserved = len(nonempty)

    remaining = n - reserved
    if remaining > 0:
        exact = [(lengths[i] / total) * remaining for i in range(parts)]
        floors = [int(math.floor(v)) for v in exact]
        leftover = remaining - sum(floors)
        order = sorted(range(parts), key=lambda i: exact[i] - floors[i], reverse=True)
        for k in range(leftover):
            floors[order[k % parts]] += 1
        counts = [counts[i] + floors[i] for i in range(parts)]

    # If n was smaller than the number of contours, give the longest contours
    # one point each rather than dropping points and breaking the count.
    if sum(counts) != n:
        counts = [0] * parts
        for i in sorted(range(parts), key=lambda i: lengths[i], reverse=True)[:n]:
            counts[i] = 1
    return counts


def _contour_length(contour: np.ndarray) -> float:
    """Closed perimeter of a polyline contour."""
    closed = contour if np.allclose(contour[0], contour[-1]) else np.vstack([contour, contour[:1]])
    return float(np.linalg.norm(np.diff(closed, axis=0), axis=1).sum())


def _sample_contours(contours: list[np.ndarray], drone_count: int) -> np.ndarray:
    """Distribute ``drone_count`` points across several closed contours by length."""
    usable = [np.asarray(c, dtype=float) for c in contours if len(c) >= 2]
    if not usable:
        raise ValueError("geometry produced no usable contours to sample")

    lengths = [_contour_length(c) for c in usable]
    counts = _allocate_by_length(lengths, drone_count)
    chunks = [_resample_closed_contour(c, k) for c, k in zip(usable, counts) if k > 0]
    if not chunks:
        raise ValueError("contour sampling produced no points")
    return np.vstack(chunks)


def contour_feasible_scale(
    contours: list[np.ndarray],
    drone_count: int,
    min_separation_m: float,
) -> float:
    """Smallest formation size that can hold ``drone_count`` points at that spacing.

    A 1D outline has a hard capacity: ``drone_count`` points spaced
    ``min_separation_m`` apart need ``drone_count * min_separation_m`` metres of
    perimeter, so the formation must be at least that many metres around. Since
    perimeter grows linearly with scale, the required scale follows directly
    from the shape's *unitless* perimeter-to-extent ratio.

    This matters because the alternative is silent deformation. 1000 drones at
    0.8 m on a 20 m "5" is not merely hard to solve - the outline is ~93 m long
    and would need ~800 m - so a relaxation pass asked to fix it just inflates
    the glyph until the spacing fits, quietly returning a 30 m formation to a
    caller who asked for 20 m. Computing the honest requirement up front lets
    :func:`voxelize` grow the scale deliberately and *report* it.

    Returns:
        The minimum scale in metres, including
        :data:`SCALE_FEASIBILITY_MARGIN`.
    """
    if drone_count <= 1 or min_separation_m <= 0:
        return 0.0

    usable = [np.asarray(c, dtype=float) for c in contours if len(c) >= 2]
    if not usable:
        return 0.0

    stacked = np.vstack(usable)
    extent = float((stacked.max(axis=0) - stacked.min(axis=0)).max())
    perimeter = float(sum(_contour_length(c) for c in usable))
    if extent <= 1e-12 or perimeter <= 1e-12:
        return 0.0

    # Perimeter per unit of extent is scale-invariant, so the required extent is
    # the required perimeter divided by that ratio.
    required_perimeter = drone_count * min_separation_m
    return (required_perimeter / (perimeter / extent)) * SCALE_FEASIBILITY_MARGIN


def sample_text_point_cloud(
    text: str,
    drone_count: int,
    scale_meters: float = 20.0,
    font_family: str = DEFAULT_FONT_FAMILY,
    font_size: float = GLYPH_FONT_SIZE,
) -> np.ndarray:
    """Sample ``drone_count`` points along the outline of ``text``.

    Glyph outlines come from matplotlib's :class:`~matplotlib.textpath.TextPath`
    - a font-vector API, not a raster one - so the result follows the true
    curve of the letterform rather than the staircase of a bitmap. Each closed
    contour, including the counters of "4", "8" or "A", is resampled at equal
    arc length and given a share of the budget proportional to its length.

    Args:
        text: String to form. Must contain at least one drawable glyph.
        drone_count: Exact number of points to return.
        scale_meters: Larger extent of the result, metres.
        font_family: Any family matplotlib's font manager can resolve.
        font_size: Outline extraction size; the cloud is rescaled afterwards.

    Returns:
        ``(drone_count, 2)`` array in the shape-local XY frame, Y-up and
        centred on the origin.

    Raises:
        ValueError: Empty text, or text with no drawable outline.
    """
    contours = _text_contours(text, font_family, font_size)
    pts = _sample_contours(contours, _validate_count(drone_count))
    return _normalize_2d(pts, scale_meters)


def _text_contours(text: str, font_family: str, font_size: float) -> list[np.ndarray]:
    """Closed glyph outlines for ``text``, in the font's own units."""
    from matplotlib.font_manager import FontProperties
    from matplotlib.textpath import TextPath

    if not text or not text.strip():
        raise ValueError("text must contain at least one non-whitespace character")

    prop = FontProperties(family=font_family, size=font_size)
    path = TextPath((0.0, 0.0), text, prop=prop)

    # to_polygons() flattens the glyph's beziers into closed polylines.
    contours = [np.asarray(p, dtype=float) for p in path.to_polygons()]
    contours = [c for c in contours if len(c) >= 2]
    if not contours:
        raise ValueError(f"text {text!r} produced no drawable glyph outlines")
    return contours


def sample_svg_point_cloud(
    svg: str,
    drone_count: int,
    scale_meters: float = 20.0,
    samples_per_segment: int = 64,
) -> np.ndarray:
    """Sample ``drone_count`` points along an SVG vector path.

    Accepts either a raw path ``d`` string (``"M 0,0 L 10,0 ..."``) or a full
    ``<svg>`` document, and handles the M/L/C/Q/Z commands via
    :mod:`svgpathtools`. Points are distributed across sub-paths in proportion
    to their curve length, so a logo's small details keep the same drone
    density as its large strokes.

    SVG's Y axis points *down* while the drone frame is Y-up, so the sampled
    outline is flipped vertically here - otherwise every imported logo would
    fly upside down.

    Args:
        svg: Path ``d`` string, or the text of an ``.svg`` file.
        drone_count: Exact number of points to return.
        scale_meters: Larger extent of the result, metres.
        samples_per_segment: Polyline resolution per curve segment before arc
            length resampling. Higher is smoother and slower.

    Returns:
        ``(drone_count, 2)`` array in the shape-local XY frame.

    Raises:
        ValueError: Empty content, or markup with no drawable path.
    """
    contours = _svg_contours(svg, samples_per_segment)
    pts = _sample_contours(contours, _validate_count(drone_count))
    return _normalize_2d(pts, scale_meters)


def _svg_contours(svg: str, samples_per_segment: int = 64) -> list[np.ndarray]:
    """Polyline contours for an SVG path string or document, Y already flipped.

    SVG's Y axis points *down* while the drone frame is Y-up, so the outline is
    negated here - otherwise every imported logo would fly upside down.
    """
    from svgpathtools import parse_path, svgstr2paths

    if not svg or not svg.strip():
        raise ValueError("svg content must not be empty")

    stripped = svg.strip()
    if stripped.startswith("<") or "<svg" in stripped.lower():
        paths, _ = svgstr2paths(stripped)
    else:
        paths = [parse_path(stripped)]

    # Each continuous sub-path becomes one contour. Splitting on
    # continuous_subpaths keeps a multi-loop logo's loops independent, so the
    # length-proportional budget applies per loop.
    contours: list[np.ndarray] = []
    for path in paths:
        for sub in path.continuous_subpaths():
            if len(sub) == 0 or sub.length() <= 1e-12:
                continue
            steps = max(2, samples_per_segment * len(sub))
            pts = np.array(
                [[sub.point(t).real, -sub.point(t).imag] for t in np.linspace(0.0, 1.0, steps)]
            )
            contours.append(pts)

    if not contours:
        raise ValueError("svg content produced no drawable paths")
    return contours


def _poisson_disk_filter(
    candidates: np.ndarray,
    n: int,
    radius: float,
    rng: np.random.Generator,
) -> np.ndarray:
    """Greedily pick ``n`` of ``candidates`` that are at least ``radius`` apart.

    A dart-throwing Poisson-disk approximation: walk a shuffled candidate pool
    and accept a point only when it clears every already-accepted point. That
    enforces the minimum-separation constraint directly, which uniform random
    surface sampling cannot do - random sampling leaves clumps that put two
    drones well inside each other's rotor wash.

    Radius elimination alone cannot guarantee a *count*, though: pack a small
    mesh tightly enough and too few candidates survive. Rather than return
    short - a show with a missing drone - the radius is relaxed geometrically
    and retried, and any final shortfall is topped up with the leftovers
    farthest from what was already accepted, keeping the extras as well spread
    as the geometry permits.
    """
    if n <= 0:
        return np.empty((0, candidates.shape[1]), dtype=float)

    pool = candidates[rng.permutation(len(candidates))]
    accepted = np.empty((0, pool.shape[1]), dtype=float)
    r = float(radius)

    # Relax the radius until n points fit. 12 halvings takes any starting
    # radius to effectively zero, at which point every candidate is accepted.
    for _ in range(12):
        accepted = np.empty((0, pool.shape[1]), dtype=float)
        r_sq = r * r
        for p in pool:
            if len(accepted) == n:
                break
            if len(accepted) == 0 or np.min(((accepted - p) ** 2).sum(axis=1)) >= r_sq:
                accepted = np.vstack([accepted, p[None, :]])
        if len(accepted) >= n:
            return accepted[:n]
        r *= 0.5

    # Still short (a mesh smaller than the pool's spread can do this): top up
    # with the leftover candidates sitting farthest from the accepted set.
    chosen = {tuple(p) for p in accepted}
    leftovers = np.array([p for p in pool if tuple(p) not in chosen])
    while len(accepted) < n and len(leftovers) > 0:
        d = np.sqrt(((leftovers[:, None, :] - accepted[None, :, :]) ** 2).sum(axis=2)).min(axis=1)
        pick = int(np.argmax(d))
        accepted = np.vstack([accepted, leftovers[pick][None, :]])
        leftovers = np.delete(leftovers, pick, axis=0)

    # A degenerate mesh can exhaust the pool entirely; repeat the last point
    # rather than break the exact-count contract. enforce_min_separation then
    # pushes the duplicates apart.
    while len(accepted) < n:
        accepted = np.vstack([accepted, accepted[-1][None, :]])
    return accepted[:n]


def sample_mesh_point_cloud(
    mesh_data,
    drone_count: int,
    scale_meters: float = 20.0,
    file_type: str | None = None,
    min_separation_m: float = DEFAULT_MIN_SEPARATION_M,
    seed: int = 0,
) -> np.ndarray:
    """Sample ``drone_count`` points across the surface of a 3D mesh.

    Loads .obj/.stl geometry with :mod:`trimesh`, draws a dense area-weighted
    candidate pool from the surface, then thins it with
    :func:`_poisson_disk_filter` so accepted points respect
    ``min_separation_m``. The mesh is scaled to ``scale_meters`` *before*
    filtering, so the radius is applied in real metres rather than in whatever
    units the CAD file happened to use.

    Args:
        mesh_data: Path to a mesh file, or its raw ``bytes``/``str`` contents.
        drone_count: Exact number of points to return.
        scale_meters: Largest dimension of the result, metres.
        file_type: ``"obj"``/``"stl"``; required when passing raw contents.
        min_separation_m: Minimum inter-point distance the filter targets.
        seed: Seeded RNG, so the same mesh always yields the same formation.

    Returns:
        ``(drone_count, 3)`` array centred on the origin.

    Raises:
        ValueError: Mesh with no faces, or raw bytes without a ``file_type``.
    """
    import trimesh

    drone_count = _validate_count(drone_count)
    if scale_meters <= 0:
        raise ValueError(f"scale_meters must be positive, got {scale_meters}")

    if isinstance(mesh_data, (bytes, bytearray)):
        if not file_type:
            raise ValueError("file_type is required when passing raw mesh bytes")
        loaded = trimesh.load(io.BytesIO(bytes(mesh_data)), file_type=file_type)
    elif isinstance(mesh_data, str) and file_type:
        loaded = trimesh.load(io.BytesIO(mesh_data.encode("utf-8")), file_type=file_type)
    else:
        loaded = trimesh.load(mesh_data, force="mesh")

    # A .obj with several objects loads as a Scene; flatten it to one surface.
    if isinstance(loaded, trimesh.Scene):
        if len(loaded.geometry) == 0:
            raise ValueError("mesh contains no geometry")
        loaded = trimesh.util.concatenate(tuple(loaded.geometry.values()))
    if not hasattr(loaded, "faces") or len(loaded.faces) == 0:
        raise ValueError("mesh contains no faces to sample")

    mesh = loaded.copy()
    mesh.vertices = mesh.vertices - mesh.bounds.mean(axis=0)  # centre on origin
    extent = float(np.max(mesh.extents))
    if extent > 1e-12:
        mesh.vertices = mesh.vertices * (scale_meters / extent)

    pool_size = int(min(POISSON_MAX_CANDIDATES, max(drone_count * POISSON_OVERSAMPLE, 64)))
    rng = np.random.default_rng(seed)
    # sample_surface weights faces by area, so the pool is already uniform over
    # the surface; the Poisson filter then imposes the spacing floor on top.
    candidates, _ = trimesh.sample.sample_surface(mesh, pool_size, seed=seed)
    return _poisson_disk_filter(
        np.asarray(candidates, dtype=float), drone_count, min_separation_m, rng
    )


def enforce_min_separation(
    points: np.ndarray,
    min_separation_m: float = DEFAULT_MIN_SEPARATION_M,
    max_passes: int = 80,
) -> np.ndarray:
    """Push apart any pair closer than ``min_separation_m``, preserving the count.

    Arc-length resampling gives even spacing *along* a contour but says nothing
    about contours that pass near each other - the two strokes of a "4" meet,
    and a glyph's counter runs close to its outer wall. Those crossings are
    where a separation violation actually shows up, so the placed cloud gets
    this relaxation pass.

    Each pass finds the offending pairs with a cKDTree - the same structure the
    validator uses for its proximity sweep, and what keeps this near-linear
    rather than O(n^2) at 1000 drones - and moves both points apart along the
    line joining them. It is a nudge, not a re-layout: displacements are small
    and symmetric, so the shape stays recognisable.

    Coincident points have no separating direction, so they are given a
    deterministic random one before being pushed.

    The pass targets ``min_separation_m * (1 + SEPARATION_EPS)`` rather than the
    bare threshold. Relaxation converges *onto* its target, so aiming exactly at
    the limit leaves points a few times 1e-5 short of it - enough to fail a
    ``>= 0.8`` check on a value that reads as 0.8 once rounded. Aiming slightly
    high makes the guarantee hold strictly.
    """
    from scipy.spatial import cKDTree

    pts = np.array(points, dtype=float)
    if len(pts) < 2 or min_separation_m <= 0:
        return pts

    target = min_separation_m * (1.0 + SEPARATION_EPS)
    rng = np.random.default_rng(0)
    for _ in range(max_passes):
        pairs = cKDTree(pts).query_pairs(target, output_type="ndarray")
        if len(pairs) == 0:
            break
        shift = np.zeros_like(pts)
        for i, j in pairs:
            delta = pts[j] - pts[i]
            dist = float(np.linalg.norm(delta))
            if dist < 1e-9:
                delta = rng.normal(size=pts.shape[1])
                dist = float(np.linalg.norm(delta)) or 1.0
            # Split the deficit between the pair, with a little overshoot so a
            # chain of near-misses converges instead of oscillating.
            step = (delta / dist) * ((target - dist) * 0.55)
            shift[i] -= step
            shift[j] += step
        pts += shift
    return pts


def project_to_plane(
    points: np.ndarray,
    plane: str = PLANE_XY,
    altitude_meters: float = 15.0,
) -> np.ndarray:
    """Place a shape-local cloud into the world frame, NED metres.

    Two orientations, both of which a show needs:

    * ``"XY"`` - a vertical billboard facing the audience. The shape's own Y
      becomes altitude, so the figure stands upright, centred on
      ``altitude_meters``, and a mesh's depth becomes NED north.
    * ``"XZ"`` - a horizontal top-down layout, read from above. The shape lies
      flat at ``altitude_meters``, and a mesh's height feeds a small altitude
      variation around it rather than being discarded.

    ``z`` is negated on output because the platform is NED throughout: 15 m up
    is ``z = -15.0``.

    Raises:
        ValueError: Wrong array shape, or an unsupported plane.
    """
    pts = np.asarray(points, dtype=float)
    if pts.ndim != 2 or pts.shape[1] not in (2, 3):
        raise ValueError(f"points must be (n, 2) or (n, 3), got shape {pts.shape}")

    plane_key = str(plane).upper()
    if plane_key not in (PLANE_XY, PLANE_XZ):
        raise ValueError(f"plane must be one of 'XY' or 'XZ', got {plane!r}")

    local_x = pts[:, 0]
    local_y = pts[:, 1]
    local_z = pts[:, 2] if pts.shape[1] == 3 else np.zeros(len(pts))

    out = np.empty((len(pts), 3), dtype=float)
    if plane_key == PLANE_XY:
        # Vertical billboard: the shape's Y is height above ground.
        out[:, 0] = local_x
        out[:, 1] = local_z
        out[:, 2] = -(altitude_meters + local_y)
    else:
        # Horizontal: the shape lies flat; a mesh's own height rides on top of
        # the requested altitude.
        out[:, 0] = local_x
        out[:, 1] = local_y
        out[:, 2] = -(altitude_meters + local_z)
    return out


def bounding_box(points: np.ndarray) -> BoundingBox:
    """Axis-aligned bounds of a placed cloud."""
    pts = np.asarray(points, dtype=float)
    if len(pts) == 0:
        raise ValueError("cannot compute a bounding box of an empty point cloud")
    mins = pts.min(axis=0)
    maxs = pts.max(axis=0)
    return BoundingBox(
        min_x=float(mins[0]), max_x=float(maxs[0]),
        min_y=float(mins[1]), max_y=float(maxs[1]),
        min_z=float(mins[2]), max_z=float(maxs[2]),
    )


def measure_min_separation(points: np.ndarray) -> float:
    """Closest-pair distance in a cloud, metres (``inf`` for fewer than 2 points)."""
    from scipy.spatial import cKDTree

    pts = np.asarray(points, dtype=float)
    if len(pts) < 2:
        return float("inf")
    # k=2 because the nearest neighbour of any point is always itself, at d=0.
    dists, _ = cKDTree(pts).query(pts, k=2)
    return float(dists[:, 1].min())


def voxelize(
    source_type: str,
    content,
    drone_count: int,
    scale_meters: float = 20.0,
    altitude_meters: float = 15.0,
    plane: str = PLANE_XY,
    min_separation_m: float = DEFAULT_MIN_SEPARATION_M,
    font_family: str = DEFAULT_FONT_FAMILY,
    font_size: float = GLYPH_FONT_SIZE,
    file_type: str | None = None,
    seed: int = 0,
    auto_scale: bool = True,
) -> dict:
    """Turn arbitrary geometry into exactly ``drone_count`` placed waypoints.

    The single entry point behind ``POST /api/voxelize``: dispatch to the right
    sampler, place the cloud with :func:`project_to_plane`, enforce the spacing
    floor, and report what was produced.

    Separation is enforced *after* projection, not before, because that is the
    frame the drones actually fly in - two points a safe distance apart on a
    flat glyph can still collapse together once a plane mapping folds an axis
    away.

    When ``auto_scale`` is set (the default) the formation is grown to whatever
    :func:`contour_feasible_scale` says the requested count and spacing need.
    A 1D outline holds only ``perimeter / min_separation_m`` drones, so 1000
    drones at 0.8 m simply do not fit on a 20 m glyph - and forcing them
    inflates the shape anyway, just silently and unevenly. Growing it
    deliberately keeps the figure proportionate, and ``scale_meters`` in the
    result reports what was actually used. Pass ``auto_scale=False`` to hold the
    requested size and accept whatever spacing the geometry allows.

    Args:
        source_type: ``"text"``, ``"svg"`` or ``"mesh"``.
        content: The string, SVG markup, or mesh path/bytes to sample.
        drone_count: Exact fleet size.
        scale_meters: Requested largest dimension of the formation, metres.
        altitude_meters: Centre altitude above ground (positive up).
        plane: ``"XY"`` vertical billboard, or ``"XZ"`` horizontal.
        min_separation_m: Spacing floor. See :data:`DEFAULT_MIN_SEPARATION_M`
            for why the default is looser than the platform's flight limits.
        font_family: Text source only.
        font_size: Text source only.
        file_type: Mesh source only; required for raw bytes.
        seed: Mesh sampling seed.
        auto_scale: Grow ``scale_meters`` when the count cannot fit at the
            requested spacing.

    Returns:
        ``{"status", "source_type", "drone_count", "plane", "scale_meters",
        "requested_scale_meters", "bounding_box", "min_separation_m",
        "coordinates"}`` where ``coordinates`` is a list of exactly
        ``drone_count`` ``{"id", "x", "y", "z"}`` dicts.

    Raises:
        ValueError: Unsupported ``source_type``/``plane``, or content that
            yields no drawable geometry.
    """
    drone_count = _validate_count(drone_count)
    if scale_meters <= 0:
        raise ValueError(f"scale_meters must be positive, got {scale_meters}")
    kind = str(source_type).lower().strip()
    applied_scale = float(scale_meters)

    if kind == "text":
        contours = _text_contours(str(content), font_family, font_size)
        if auto_scale:
            applied_scale = max(
                applied_scale, contour_feasible_scale(contours, drone_count, min_separation_m)
            )
        local = _normalize_2d(_sample_contours(contours, drone_count), applied_scale)
    elif kind == "svg":
        contours = _svg_contours(str(content))
        if auto_scale:
            applied_scale = max(
                applied_scale, contour_feasible_scale(contours, drone_count, min_separation_m)
            )
        local = _normalize_2d(_sample_contours(contours, drone_count), applied_scale)
    elif kind == "mesh":
        local = sample_mesh_point_cloud(
            content, drone_count, applied_scale,
            file_type=file_type, min_separation_m=min_separation_m, seed=seed,
        )
        if auto_scale:
            # A surface's capacity grows with area (scale^2), not perimeter, and
            # the achievable spacing depends on the model's own topology, so
            # rather than predict it, measure what the Poisson filter delivered
            # and scale up by the shortfall ratio.
            achieved = measure_min_separation(local)
            if np.isfinite(achieved) and 0.0 < achieved < min_separation_m:
                applied_scale *= (min_separation_m / achieved) * SCALE_FEASIBILITY_MARGIN
                local = sample_mesh_point_cloud(
                    content, drone_count, applied_scale,
                    file_type=file_type, min_separation_m=min_separation_m, seed=seed,
                )
    else:
        raise ValueError(
            f"source_type must be one of 'text', 'svg', 'mesh', got {source_type!r}"
        )

    placed = project_to_plane(local, plane=plane, altitude_meters=altitude_meters)
    placed = enforce_min_separation(placed, min_separation_m)

    # A tall formation centred on a low altitude reaches below z=0, i.e.
    # underground. Nothing downstream would catch that - the validator checks
    # separation and speed, not ground clearance - so lift the whole cloud
    # until its lowest drone clears MIN_GROUND_CLEARANCE_M. Lifting rigidly
    # rather than clamping each point keeps the shape intact; clamping would
    # flatten everything below ground onto one plane and destroy both the
    # figure and the separation just established.
    lowest = float((-placed[:, 2]).min())
    if lowest < MIN_GROUND_CLEARANCE_M:
        placed[:, 2] -= MIN_GROUND_CLEARANCE_M - lowest

    if not np.all(np.isfinite(placed)):
        raise ValueError("sampling produced non-finite coordinates")

    coordinates = [
        {"id": i, "x": round(float(p[0]), 4), "y": round(float(p[1]), 4), "z": round(float(p[2]), 4)}
        for i, p in enumerate(placed)
    ]
    # Measure on the rounded output, since that is what the caller flies.
    rounded = np.array([[c["x"], c["y"], c["z"]] for c in coordinates], dtype=float)

    return {
        "status": "success",
        "source_type": kind,
        "drone_count": len(coordinates),
        "plane": str(plane).upper(),
        "scale_meters": round(applied_scale, 4),
        "requested_scale_meters": round(float(scale_meters), 4),
        "bounding_box": bounding_box(rounded).as_dict(),
        "min_separation_m": round(measure_min_separation(rounded), 4),
        "coordinates": coordinates,
    }
