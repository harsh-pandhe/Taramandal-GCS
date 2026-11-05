"""Raster image (PNG/JPG/WebP) to formation geometry (issue #90, first slice).

Takes the bytes of an untrusted image and returns exactly ``n_drones`` formation
points in the same ``{"id", "x", "y", "z"}`` NED representation
:func:`tara_studio.voxelizer.voxelize` produces. It emits **geometry only**: no
trajectories, no timing, no new show representation. The caller feeds the
points to the existing assignment/morph pipeline like any other formation.

Pipeline: validate bytes -> binary mask (alpha, else Otsu/fixed threshold) ->
silhouette contours -> arc-length resample to N -> fit to the site -> verify.
Everything is deterministic: same bytes + parameters give identical output.

The site radius, separation and altitude limits come from
:data:`tara_core.DEFAULT_LIMITS`; a caller may only be *stricter* than them.
A shape that cannot fit raises :class:`RasterFormationError` rather than being
shrunk below the limits.
"""

from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass

import numpy as np
from PIL import Image, UnidentifiedImageError

from tara_core import DEFAULT_LIMITS

from .voxelizer import (
    MIN_GROUND_CLEARANCE_M,
    PLANE_XY,
    SEPARATION_EPS,
    _contour_length,
    _sample_contours,
    bounding_box,
    enforce_min_separation,
    measure_min_separation,
    project_to_plane,
)

#: Fleet sizes this importer supports (issue #90).
MAX_RASTER_DRONES = 100

#: Upload ceilings. The byte cap bounds memory before decoding; the dimension
#: and pixel caps stop a small, highly compressed file (a decompression bomb)
#: from expanding into gigabytes. Both are checked from the header, before the
#: pixel data is decoded.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_DIMENSION = 4096
MAX_IMAGE_PIXELS = 16_000_000

#: The mask is downsampled to at most this many pixels on a side. Contour
#: precision beyond a few hundred pixels is far below the metres a drone
#: formation cares about.
MASK_MAX_SIDE = 1024

#: At most this many contours are kept, longest first (by vertex count, which is
#: cheap), before any per-contour work. A silhouette has a handful; a checkerboard
#: or noise field has hundreds of thousands, and the per-contour length and
#: resampling work below is Python-level: an unbounded count turned a few-KB PNG
#: into tens of seconds of CPU.
MAX_CONTOURS = 1000

#: An image with more than this many times MAX_CONTOURS separate shapes is refused
#: ("no clear silhouette") rather than reduced to its 1000 longest. A logo has a
#: handful of contours, a headline a few dozen, a speckled or noisy-JPEG logo a few
#: thousand; a photo, a noise field or a page of dense text has tens of thousands,
#: and a formation made from the longest 1000 of those is valid but meaningless.
REFUSE_CONTOURS_FACTOR = 10

#: If the requested scale is below this fraction of what the site allows, the
#: search for a scale that holds the count starts here instead of at the request.
#: Growing from 1e-300 by 15% a step is thousands of pointless attempts, and the
#: result is the same: the smallest scale that fits is nowhere near that small.
MIN_START_SCALE_FRACTION = 1e-3

#: Alpha above this counts as shape when the image has real transparency.
ALPHA_THRESHOLD = 128

#: Contours shorter than this fraction of the longest one are noise (JPEG
#: speckle, stray pixels) and are dropped.
MIN_CONTOUR_FRACTION = 0.02

#: Chord distance between neighbouring points on a curve is shorter than the
#: arc distance between them, so the outline is only trusted to hold
#: ``perimeter / (min_separation * OUTLINE_SLACK)`` drones; the rest go inside.
OUTLINE_SLACK = 1.1

#: Multiplier applied to the scale each time the count does not fit.
SCALE_GROWTH_STEP = 1.15

#: Candidate grid spacing for interior points, as a fraction of the separation.
INNER_GRID_FRACTION = 0.5

#: Modes Image.reduce() cannot handle (it raises "image has wrong mode"); they are
#: converted first. Everything else reduces as it always has.
_NO_REDUCE_MODES = {"1", "P", "PA", "I", "I;16", "I;16L", "I;16B", "F"}

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP"}


class RasterFormationError(ValueError):
    """The image could not be turned into a flyable formation."""


@dataclass(frozen=True)
class _Mask:
    mask: np.ndarray  # (rows, cols) bool, True = shape
    source: str  # "alpha" or "threshold"
    threshold: int | None


def _sniff_format(data: bytes) -> str:
    """Identify the container from its magic bytes, never from a filename."""
    if data.startswith(_PNG_MAGIC):
        return "PNG"
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    raise RasterFormationError(
        "unsupported image format: only PNG, JPEG and WebP are accepted "
        "(file signature does not match any of them)"
    )


_OBJECT_REPR = re.compile(r"<[^<>]*\bobject at 0x[0-9a-fA-F]+>")


def _scrub(exc: BaseException) -> str:
    """A decoder's message with interpreter object reprs removed.

    Pillow's "cannot identify image file <_io.BytesIO object at 0x7f...>" would
    otherwise put a memory address in the response body.
    """
    return _OBJECT_REPR.sub("the uploaded data", str(exc))


def _open_image(data: bytes) -> Image.Image:
    """Validate untrusted bytes and return a fully decoded, size-bounded image."""
    if not isinstance(data, (bytes, bytearray)):
        raise RasterFormationError("image data must be bytes")
    if len(data) == 0:
        raise RasterFormationError("image is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise RasterFormationError(
            f"image is {len(data)} bytes, over the {MAX_IMAGE_BYTES} byte limit"
        )
    data = bytes(data)
    expected = _sniff_format(data)

    try:
        img = Image.open(io.BytesIO(data), formats=sorted(_ALLOWED_FORMATS))
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise RasterFormationError(f"image could not be decoded: {_scrub(exc)}") from exc

    if img.format != expected:
        raise RasterFormationError(
            f"image content is {img.format}, which does not match its {expected} signature"
        )
    width, height = img.size
    if width < 1 or height < 1:
        raise RasterFormationError("image has no pixels")
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise RasterFormationError(
            f"image is {width}x{height}, over the {MAX_IMAGE_DIMENSION}px per-side limit"
        )
    if width * height > MAX_IMAGE_PIXELS:
        raise RasterFormationError(
            f"image has {width * height} pixels, over the {MAX_IMAGE_PIXELS} pixel limit"
        )
    if getattr(img, "n_frames", 1) > 1:
        raise RasterFormationError("animated or multi-frame images are not supported")

    try:
        img.load()
    except (Image.DecompressionBombError, OSError, ValueError, SyntaxError) as exc:
        raise RasterFormationError(f"image could not be decoded: {_scrub(exc)}") from exc
    return img


def _otsu_threshold(gray: np.ndarray) -> int:
    """Otsu's threshold of a uint8 image: the cut maximising between-class variance."""
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    levels = np.arange(256, dtype=np.float64)
    weight_bg = np.cumsum(hist)
    weight_fg = total - weight_bg
    sum_all = float((hist * levels).sum())
    sum_bg = np.cumsum(hist * levels)
    with np.errstate(divide="ignore", invalid="ignore"):
        mean_bg = sum_bg / weight_bg
        mean_fg = (sum_all - sum_bg) / weight_fg
        between = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
    between = np.nan_to_num(between, nan=-1.0)
    # argmax returns the first maximum, so ties resolve deterministically.
    return int(np.argmax(between))


def _binary_mask(img: Image.Image, threshold: int | None) -> _Mask:
    """Shape mask: alpha when the image is really transparent, else a threshold."""
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (
        img.mode == "P" and "transparency" in img.info
    )
    if max(img.size) > MASK_MAX_SIDE:
        if img.mode in _NO_REDUCE_MODES:
            # The same conversion the code below applies afterwards, done first.
            img = img.convert("RGBA" if has_alpha else "L")
        factor = math.ceil(max(img.size) / MASK_MAX_SIDE)
        img = img.reduce(factor)  # box filter, deterministic
    if has_alpha:
        alpha = np.asarray(img.convert("RGBA"))[:, :, 3]
        if alpha.min() < 255:
            return _Mask(alpha >= ALPHA_THRESHOLD, "alpha", ALPHA_THRESHOLD)

    gray = np.asarray(img.convert("L"))
    if threshold is None:
        cut = _otsu_threshold(gray)
    else:
        cut = int(threshold)
    bright = gray > cut
    # The background is whatever dominates the border, so dark-on-light and
    # light-on-dark logos both come out with the logo as the shape.
    border = np.concatenate([bright[0], bright[-1], bright[:, 0], bright[:, -1]])
    mask = ~bright if border.mean() >= 0.5 else bright
    return _Mask(mask, "threshold", cut)


def _extract_contours(mask: np.ndarray) -> list[np.ndarray]:
    """Closed silhouette polylines in pixel coordinates (x=col, y=row).

    Marching squares on the zero-padded mask at level 0.5, so every contour
    closes inside the frame and edges sit between pixel centres. Contours of
    holes are included: the outline of a ring is two circles.
    """
    import contourpy

    padded = np.pad(mask.astype(np.float64), 1)
    lines = [line for line in contourpy.contour_generator(z=padded).lines(0.5) if len(line) >= 3]
    if len(lines) > MAX_CONTOURS * REFUSE_CONTOURS_FACTOR:
        raise RasterFormationError(
            f"image has no clear silhouette: {len(lines)} separate shapes were found "
            f"(more than {MAX_CONTOURS * REFUSE_CONTOURS_FACTOR}). Use a simple, high-contrast "
            "image with a distinct outline, or raise the threshold to cut the background noise"
        )
    if len(lines) > MAX_CONTOURS:
        sizes = np.fromiter((len(line) for line in lines), dtype=np.int64, count=len(lines))
        keep = np.sort(np.argsort(-sizes, kind="stable")[:MAX_CONTOURS])  # longest first, original order kept
        lines = [lines[i] for i in keep]
    contours = [np.asarray(line, dtype=float) - 1.0 for line in lines]
    if not contours:
        return []
    lengths = [_contour_length(c) for c in contours]
    keep = max(lengths) * MIN_CONTOUR_FRACTION
    return [c for c, ln in zip(contours, lengths) if ln >= keep]


def _fit_limits(
    half_w: float,
    half_h: float,
    center_x: float,
    center_alt: float,
    site_radius_m: float,
    max_altitude_m: float,
) -> float:
    """Largest scale (metres, largest dimension) at which the shape stays on site.

    ``half_w`` / ``half_h`` are the shape's half extents per metre of scale. The
    formation is a vertical billboard on the y=0 line, so horizontal reach is
    ``|x|`` and the vertical limits are the ground clearance and the ceiling.
    """
    limit = math.inf
    if half_w > 0:
        limit = min(limit, (site_radius_m - abs(center_x)) / half_w)
    if half_h > 0:
        limit = min(limit, (center_alt - MIN_GROUND_CLEARANCE_M) / half_h)
        limit = min(limit, (max_altitude_m - center_alt) / half_h)
    return limit


def _interior_points(
    mask: np.ndarray,
    to_local,
    pixel_m: float,
    existing: np.ndarray,
    count: int,
    min_sep: float,
) -> np.ndarray:
    """``count`` deterministic points inside the shape, each ``min_sep`` from all others.

    Greedy farthest-point sampling over a grid of in-mask candidates: each pick
    is the candidate furthest from everything placed so far, which spreads the
    fill evenly and keeps it clear of the outline.
    """
    from scipy.spatial import cKDTree

    # Clamped to the mask: a tiny scale (hence a tiny pixel size) made this an
    # int too large for numpy's indexing arithmetic.
    step = max(1, min(int(round(min_sep * INNER_GRID_FRACTION / pixel_m)), max(mask.shape)))
    rows, cols = np.nonzero(mask[::step, ::step])
    candidates = to_local(np.column_stack([cols * step, rows * step]).astype(float))
    if len(candidates) == 0:
        raise RasterFormationError("shape has no interior to place the remaining drones in")

    target = min_sep * (1.0 + SEPARATION_EPS)
    dist = cKDTree(existing).query(candidates, k=1)[0]
    picked = []
    for _ in range(count):
        best = int(np.argmax(dist))
        if dist[best] < target:
            raise RasterFormationError(
                f"shape is too small to hold {len(existing) + count} drones at "
                f"{min_sep:.2f} m separation within the site limits"
            )
        point = candidates[best]
        picked.append(point)
        dist = np.minimum(dist, np.linalg.norm(candidates - point, axis=1))
    return np.array(picked)


def _build_at_scale(
    applied: float,
    n: int,
    min_sep: float,
    unit_contours: list[np.ndarray],
    unit_perimeter: float,
    mask: np.ndarray,
    to_unit,
    extent_px: float,
) -> tuple[np.ndarray, int, int]:
    """Shape-local points (outline first, then interior) at one candidate scale."""
    capacity = 1 if n == 1 else int((unit_perimeter * applied) // (min_sep * OUTLINE_SLACK))
    n_outline = min(n, max(capacity, 1))
    n_inner = n - n_outline

    outline = _sample_contours([c * applied for c in unit_contours], n_outline)
    if n_outline > 1:
        outline = enforce_min_separation(outline, min_sep)
    if not n_inner:
        return outline, n_outline, 0
    inner = _interior_points(
        mask,
        lambda px: to_unit(px) * applied,
        pixel_m=applied / extent_px,
        existing=outline,
        count=n_inner,
        min_sep=min_sep,
    )
    return np.vstack([outline, inner]), n_outline, n_inner


def raster_to_formation(
    image_bytes: bytes,
    n_drones: int,
    scale: float = 20.0,
    center: tuple[float, float] = (0.0, 15.0),
    threshold: int | None = None,
    site_radius_m: float | None = None,
    min_separation_m: float | None = None,
) -> dict:
    """Turn a PNG/JPG/WebP silhouette into exactly ``n_drones`` formation points.

    Args:
        image_bytes: Raw, untrusted file contents.
        n_drones: Fleet size, 1 to :data:`MAX_RASTER_DRONES`.
        scale: Requested largest dimension of the formation, metres. It is only
            reduced as far as the site limits force, or grown (in steps, up to
            the site limit) when the count cannot be held at the required
            spacing; the applied value is reported as ``scale_meters``.
        center: ``(x_m, altitude_m)`` of the bounding-box centre. The formation
            is a vertical billboard on the y=0 line, as in ``voxelize``'s XY plane.
        threshold: 0-255 cut for opaque images; ``None`` picks Otsu's. Ignored
            when the image has real transparency (alpha is used instead).
        site_radius_m: Horizontal reach limit. Defaults to, and may not exceed,
            ``DEFAULT_LIMITS.geofence_radius_m``.
        min_separation_m: Pairwise spacing floor. Defaults to, and may not go
            below, ``DEFAULT_LIMITS.design_separation_m``.

    Returns:
        A ``voxelize``-shaped dict (``coordinates`` is ``[{"id","x","y","z"}]``,
        NED metres) plus raster details: ``mask_source``, ``threshold``,
        ``contour_vertices``, ``outline_points``, ``inner_points_added`` and
        ``vertices_dropped`` (contour vertices not needed after resampling).

    Raises:
        RasterFormationError: Bad or unsupported image, blank image, or a shape
            that cannot fit the limits. Never a silently shrunk result.
    """
    limits = DEFAULT_LIMITS
    site_radius = limits.geofence_radius_m if site_radius_m is None else float(site_radius_m)
    min_sep = limits.design_separation_m if min_separation_m is None else float(min_separation_m)

    if isinstance(n_drones, bool) or not isinstance(n_drones, (int, np.integer)):
        raise RasterFormationError(f"n_drones must be an integer, got {n_drones!r}")
    n = int(n_drones)
    if not 1 <= n <= MAX_RASTER_DRONES:
        raise RasterFormationError(f"n_drones must be 1-{MAX_RASTER_DRONES}, got {n}")
    if not (math.isfinite(scale) and scale > 0):
        raise RasterFormationError(f"scale must be positive and finite, got {scale!r}")
    if not (math.isfinite(site_radius) and 0 < site_radius <= limits.geofence_radius_m):
        raise RasterFormationError(
            f"site_radius_m must be in (0, {limits.geofence_radius_m}], got {site_radius_m!r}"
        )
    if not (math.isfinite(min_sep) and min_sep >= limits.design_separation_m):
        raise RasterFormationError(
            f"min_separation_m may not be below the {limits.design_separation_m} m "
            f"design separation limit, got {min_separation_m!r}"
        )
    cx, alt = float(center[0]), float(center[1])
    if not (math.isfinite(cx) and math.isfinite(alt)):
        raise RasterFormationError(f"center must be finite, got {center!r}")
    if threshold is not None and not 0 <= int(threshold) <= 255:
        raise RasterFormationError(f"threshold must be 0-255, got {threshold!r}")

    img = _open_image(image_bytes)
    shape = _binary_mask(img, threshold)
    coverage = float(shape.mask.mean())
    if coverage == 0.0 or coverage == 1.0:
        raise RasterFormationError("image is blank: no shape distinguishable from the background")

    contours = _extract_contours(shape.mask)
    if not contours:
        raise RasterFormationError("no usable silhouette found in the image")

    # Pixel space -> shape-local metres (y up), centred on the outline's bbox
    # and scaled uniformly so the largest dimension is 1 per unit scale.
    stacked = np.vstack(contours)
    mins, maxs = stacked.min(axis=0), stacked.max(axis=0)
    extent_px = float((maxs - mins).max())
    if extent_px <= 1e-9:
        raise RasterFormationError("silhouette is degenerate (zero size)")
    mid = (mins + maxs) / 2.0

    def to_unit(px: np.ndarray) -> np.ndarray:
        out = (np.asarray(px, dtype=float) - mid) / extent_px
        out[:, 1] *= -1.0  # image rows run down, altitude runs up
        return out

    half_w = float((maxs[0] - mins[0]) / 2.0 / extent_px)
    half_h = float((maxs[1] - mins[1]) / 2.0 / extent_px)

    max_scale = _fit_limits(half_w, half_h, cx, alt, site_radius, limits.max_altitude_m)
    if not max_scale > 0:
        raise RasterFormationError(
            f"center {center!r} leaves no room inside the site (radius {site_radius} m, "
            f"altitude {MIN_GROUND_CLEARANCE_M}-{limits.max_altitude_m} m)"
        )
    unit_contours = [to_unit(c) for c in contours]
    unit_perimeter = sum(_contour_length(c) for c in unit_contours)
    ceiling = max_scale * (1.0 - 1e-6)

    # Try the requested size first. If the count cannot be held at the required
    # spacing, grow in fixed steps up to what the site allows (never beyond),
    # and report the size actually used. Failure at the ceiling is final.
    applied = min(max(float(scale), ceiling * MIN_START_SCALE_FRACTION), ceiling)
    while True:
        try:
            local, n_outline, n_inner = _build_at_scale(
                applied, n, min_sep, unit_contours, unit_perimeter, shape.mask,
                to_unit, extent_px,
            )
            placed = project_to_plane(local, plane=PLANE_XY, altitude_meters=alt)
            placed[:, 0] += cx
            coordinates = [
                {"id": i, "x": round(float(p[0]), 4), "y": round(float(p[1]), 4), "z": round(float(p[2]), 4)}
                for i, p in enumerate(placed)
            ]
            rounded = np.array([[c["x"], c["y"], c["z"]] for c in coordinates], dtype=float)
            _verify(rounded, n, site_radius, min_sep, limits.max_altitude_m)
            break
        except RasterFormationError:
            if applied >= ceiling:
                raise
            applied = min(applied * SCALE_GROWTH_STEP, ceiling)

    return {
        "status": "success",
        "source_type": "raster",
        "drone_count": n,
        "plane": PLANE_XY,
        "scale_meters": round(applied, 4),
        "requested_scale_meters": round(float(scale), 4),
        "bounding_box": bounding_box(rounded).as_dict(),
        "min_separation_m": round(measure_min_separation(rounded), 4),
        "coordinates": coordinates,
        "mask_source": shape.source,
        "threshold": shape.threshold,
        "contour_vertices": int(sum(len(c) for c in contours)),
        "outline_points": n_outline,
        "inner_points_added": n_inner,
        "vertices_dropped": max(0, int(sum(len(c) for c in contours)) - n_outline),
    }


def _verify(points: np.ndarray, n: int, site_radius: float, min_sep: float, max_alt: float) -> None:
    """Final gate on the rounded output, which is what would actually be flown."""
    if len(points) != n or not np.all(np.isfinite(points)):
        raise RasterFormationError("formation produced a wrong or non-finite point set")
    reach = float(np.hypot(points[:, 0], points[:, 1]).max())
    if reach > site_radius:
        raise RasterFormationError(
            f"formation reaches {reach:.2f} m from the origin, past the {site_radius:.2f} m site radius"
        )
    alt = -points[:, 2]
    if alt.min() < MIN_GROUND_CLEARANCE_M - 1e-9 or alt.max() > max_alt:
        raise RasterFormationError(
            f"formation altitude {alt.min():.2f}-{alt.max():.2f} m is outside "
            f"{MIN_GROUND_CLEARANCE_M}-{max_alt} m"
        )
    achieved = measure_min_separation(points)
    if achieved < min_sep:
        raise RasterFormationError(
            f"cannot fit {n} drones at {min_sep:.2f} m separation inside the site limits "
            f"(closest pair {achieved:.2f} m); use fewer drones or a simpler/larger shape"
        )
