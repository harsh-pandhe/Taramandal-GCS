"""
Taramandal Studio - FastAPI Server
Exposes API endpoints for generating, validating, and exporting drone shows.
"""

import base64
from dataclasses import asdict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional

from tara_studio.generator import (
    generate_circle,
    generate_helix,
    generate_heart,
    generate_cube,
    generate_spline,
    plan_show_pacing,
)
from tara_studio.validator import (
    DEFAULT_DYNAMIC_LIMITS,
    DynamicLimits,
    validate_choreography,
    validate_dynamics,
)
from tara_studio.exporter import export_json, export_csv, export_kml, export_tara
from tara_studio.led_engine import (
    apply_track_to_choreo,
    led_sample_rate_hz,
    upsample_choreo,
    upsample_factor,
)
from tara_studio.optimizer import (
    DEFAULT_MORPH_SAMPLES,
    DEFAULT_PENALTY_WEIGHT,
    DOWNWASH_HALF_ANGLE_DEG,
    aerodynamic_assignment,
)
from tara_studio.voxelizer import (
    DEFAULT_FONT_FAMILY,
    DEFAULT_MIN_SEPARATION_M,
    GLYPH_FONT_SIZE,
    voxelize,
)
from tara_core import DEFAULT_LIMITS, MAX_FLEET_SIZE
from tara_core.assignment import optimal_assignment

app = FastAPI(title="Taramandal Studio Choreography Engine", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Open API access for local dashboard utility
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class GenerateRequest(BaseModel):
    shape: str = Field("circle", description="Trajectory pattern: circle, helix, heart, cube")
    # Was capped at 10 — a hard blocker for the 100-drone shows this platform
    # targets, since Pydantic rejected the request outright before it ever
    # reached the generator. Now matches tara_core.MAX_FLEET_SIZE, the same
    # ceiling the GCS's own pad grid/altitude layering is built for.
    num_drones: int = Field(3, ge=1, le=MAX_FLEET_SIZE, description="Number of drones")
    duration: float = Field(30.0, ge=10.0, le=120.0, description="Duration in seconds")
    rate: float = Field(2.0, ge=1.0, le=10.0, description="Sampling rate (Hz)")
    spawn_offsets: Optional[list[float]] = Field(None, description="Custom spawn offsets per drone")
    target_heights: Optional[list[float]] = Field(None, description="Custom target hover heights per drone")

class ExportRequest(BaseModel):
    choreo: dict = Field(..., description="Choreography dataset")
    format: str = Field("json", description="Export file format: json, csv, kml, tara")
    home_lat: Optional[float] = Field(12.9716, description="Home latitude for KML maps")
    home_lon: Optional[float] = Field(77.5946, description="Home longitude for KML maps")
    led_track: Optional[list[dict]] = Field(
        None, description="LED keyframes to bake into the export (see led_engine.LedKeyframe)"
    )
    rate_hz: Optional[float] = Field(0.0, description="Sample rate recorded in the .tara header")

def _with_limits(report: dict) -> dict:
    """Attach the SafetyLimits the report was judged against (issue #97).

    The Studio UI colours each metric pass/fail. It used to do that against its
    own hardcoded thresholds, which drifted from tara_core (0.8m separation
    against a 1.5m runtime abort). Sending the live limits with every report
    means the UI has no copy of its own to drift. validate_choreography() with
    no overrides validates against exactly DEFAULT_LIMITS, so that is what goes
    out.
    """
    return {**report, "limits": asdict(DEFAULT_LIMITS)}


@app.get("/status")
def status():
    return {"status": "ONLINE", "engine": "Taramandal-Studio-v1"}

#: Shape name -> generator. All five share one signature, so the endpoint
#: dispatches through this instead of an if/elif chain that repeated the same
#: five arguments per branch.
_GENERATORS = {
    "circle": generate_circle,
    "helix": generate_helix,
    "heart": generate_heart,
    "cube": generate_cube,
    "spline": generate_spline,
}

@app.post("/api/generate")
def api_generate(req: GenerateRequest):
    shape = req.shape.lower()

    generate = _GENERATORS.get(shape)
    if generate is None:
        raise HTTPException(status_code=400, detail=f"Unsupported shape format: {req.shape}")

    try:
        choreo = generate(req.num_drones, req.duration, req.rate, req.spawn_offsets, req.target_heights)
    except ValueError as e:
        # A mismatched spawn_offsets/target_heights override is a bad request,
        # not a server fault. Pydantic can't catch it: the valid length depends
        # on num_drones, another field. Before this it escaped as an IndexError
        # from inside the shape's waypoint loop and surfaced as a blank 500.
        raise HTTPException(status_code=400, detail=str(e))

    # Perform safety validation check automatically
    validation = _with_limits(validate_choreography(choreo))

    # Report the pacing the generator chose. The show's length is no longer
    # whatever `duration` said — it is stretched to whatever the fleet size can
    # actually fly safely — so the UI needs to be told, rather than left to
    # assume its request was honoured verbatim.
    pacing = plan_show_pacing(
        req.num_drones, shape, req.spawn_offsets, req.target_heights
    )
    actual_s = max(
        (d["waypoints"][-1]["time"] for d in choreo["drones"] if d["waypoints"]),
        default=0.0,
    )

    return {
        "choreo": choreo,
        "validation": validation,
        "pacing": {
            "multiplier": round(pacing.multiplier, 2),
            "takeoff_s": round(pacing.takeoff_s, 2),
            "choreo_s": round(pacing.choreo_s, 2),
            "land_s": round(pacing.land_s, 2),
            # The pacing floor, versus the length actually generated — they
            # differ when `duration` asks for more than the floor requires.
            "minimum_s": round(pacing.total_s, 2),
            "total_s": round(actual_s, 2),
            "requested_duration_s": req.duration,
        },
    }

@app.post("/api/validate")
def api_validate(choreo: dict):
    return _with_limits(validate_choreography(choreo))


class VoxelizeRequest(BaseModel):
    source_type: str = Field("text", description="Geometry source: text, svg, mesh")
    content: str = Field(
        ...,
        description=(
            "Text to form, SVG path/document markup, or a path to an .obj/.stl file"
        ),
    )
    drone_count: int = Field(
        100, ge=1, le=MAX_FLEET_SIZE, description="Exact number of points to return"
    )
    scale_meters: float = Field(
        20.0, gt=0, le=1000.0, description="Requested largest formation dimension, m"
    )
    altitude_meters: float = Field(
        15.0, ge=0, le=120.0, description="Centre altitude above ground, m (positive up)"
    )
    plane: str = Field("XY", description="XY vertical billboard, or XZ horizontal")
    min_separation_m: float = Field(
        DEFAULT_MIN_SEPARATION_M, gt=0, description="Minimum inter-drone distance, m"
    )
    font_family: str = Field(DEFAULT_FONT_FAMILY, description="Font family (text source)")
    font_size: float = Field(
        GLYPH_FONT_SIZE, gt=0, description="Glyph extraction size (text source)"
    )
    file_type: Optional[str] = Field(
        None, description="Mesh format override: obj or stl"
    )
    auto_scale: bool = Field(
        True,
        description=(
            "Grow scale_meters when drone_count cannot fit at min_separation_m"
        ),
    )


@app.post("/api/voxelize")
def api_voxelize(req: VoxelizeRequest):
    """
    Rasterize arbitrary geometry into exactly `drone_count` drone positions.

    Covers the shapes the six parametric generators in /api/generate cannot:
    arbitrary strings ("5", "INDIA"), SVG logo paths, and .obj/.stl meshes.
    Points are placed by measured geometry rather than by curve parameter —
    equal arc length along an outline, Poisson-disk across a mesh surface — so
    drones are spread evenly instead of bunching wherever the source curve
    happens to be dense.

    Returns a static point cloud, not a timed choreography: the result is one
    formation, which the caller feeds to /api/assign to morph into. `plane`
    picks the orientation — "XY" stands the shape up as a billboard facing the
    audience, "XZ" lays it flat to be read from above.

    Two fields are worth reading in the response. `min_separation_m` is the
    spacing actually achieved, measured on the rounded output; and
    `scale_meters` is the size actually used, which exceeds
    `requested_scale_meters` when `auto_scale` had to grow the formation — an
    outline of perimeter L holds only L / min_separation_m drones, so a large
    fleet on a small glyph does not fit at any spacing.

    Note the default `min_separation_m` of 0.8 m is looser than the platform's
    own flight limits (tara_core.safety: 1.5 m runtime abort, 2.0 m design).
    Pass 2.0 for a formation intended to actually fly.
    """
    try:
        return voxelize(
            source_type=req.source_type,
            content=req.content,
            drone_count=req.drone_count,
            scale_meters=req.scale_meters,
            altitude_meters=req.altitude_meters,
            plane=req.plane,
            min_separation_m=req.min_separation_m,
            font_family=req.font_family,
            font_size=req.font_size,
            file_type=req.file_type,
            auto_scale=req.auto_scale,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=f"mesh file not found: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

class AssignRequest(BaseModel):
    from_pts: list = Field(..., alias="from", description="Current formation points [{x,y,z}]")
    to_pts: list = Field(..., alias="to", description="Target formation points [{x,y,z}]")

    class Config:
        populate_by_name = True

@app.post("/api/assign")
def api_assign(req: AssignRequest):
    """
    Returns the minimum-travel drone→target assignment between two equal-size
    formations, used to make formation morphs collision-free.
    Response: {"assignment": [target_index_for_source_0, ...]}.
    """
    try:
        perm = optimal_assignment(req.from_pts, req.to_pts)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"assignment": perm}


class AssignChainRequest(BaseModel):
    forms: list[list[dict]] = Field(
        ..., description="One point-set [{x,y,z}, ...] per formation in sequence, 2+ formations"
    )


@app.post("/api/assign-chain")
def api_assign_chain(req: AssignChainRequest):
    """
    Computes the full collision-free assignment chain across an entire show's
    formation sequence in one request, instead of the sequencer making one
    request per consecutive formation pair.

    The chain is inherently sequential — the assignment into formation s+1
    depends on which point each drone occupies in formation s, which depends
    on the assignment before it — so those requests could never be
    parallelized on the client; the previous per-pair round trips still had
    to happen one after another. Computing the whole chain server-side
    instead collapses N-1 sequential network round trips (each paying full
    HTTP/TCP overhead) into exactly 1, which is what actually matters as a
    show's formation count grows for a larger fleet, not the per-request
    Hungarian-algorithm cost itself (still comfortably sub-millisecond at
    100 drones).

    Response: {"target_idx": [[int, ...], ...]} — one permutation per
    formation, target_idx[0] is the identity (drones start at their own
    index in formation 0).
    """
    if len(req.forms) < 1:
        raise HTTPException(status_code=400, detail="forms must contain at least one formation")

    n = len(req.forms[0])
    for i, form in enumerate(req.forms):
        if len(form) != n:
            raise HTTPException(
                status_code=400,
                detail=f"formation {i} has {len(form)} points, expected {n} (all formations must be equal size)",
            )

    target_idx = [list(range(n))]
    for s in range(len(req.forms) - 1):
        current = target_idx[s]
        from_pts = [req.forms[s][current[i]] for i in range(n)]
        try:
            perm = optimal_assignment(from_pts, req.forms[s + 1])
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        target_idx.append(perm)

    return {"target_idx": target_idx}


class AeroAssignRequest(BaseModel):
    from_pts: list = Field(..., alias="from", description="Current formation points [{x,y,z}]")
    to_pts: list = Field(..., alias="to", description="Target formation points [{x,y,z}]")
    penalty_weight: float = Field(
        DEFAULT_PENALTY_WEIGHT, gt=0, description="Downwash penalty multiplier (lambda)"
    )
    samples: int = Field(
        DEFAULT_MORPH_SAMPLES, ge=2, le=201, description="Morph-horizon sample count"
    )
    half_angle_deg: float = Field(
        DOWNWASH_HALF_ANGLE_DEG, gt=0, lt=90, description="Downwash cone half-angle, degrees"
    )

    class Config:
        populate_by_name = True


@app.post("/api/assign-aero")
def api_assign_aero(req: AeroAssignRequest):
    """
    Assignment that avoids rotor downwash, not just distance.

    /api/assign minimises travel and will happily route one drone directly
    beneath another — the paths never intersect, so a purely geometric solver
    sees nothing wrong, but the lower drone flies into a >10 m/s jet and drops.
    This endpoint adds the 30-degree cone penalty described in
    tara_studio.optimizer and returns what the morph actually costs.

    A non-empty `conflicts` with `clean: false` means no conflict-free
    assignment exists for these two formations — most often because the
    starting formation already stacks one drone above another, which no
    reassignment can repair. That is reported rather than raised, so the caller
    can decide whether to re-space the formation or accept the risk.

    Response: {"assignment", "clean", "conflicts", "total_distance_m",
    "iterations"}.
    """
    try:
        report = aerodynamic_assignment(
            req.from_pts,
            req.to_pts,
            penalty_weight=req.penalty_weight,
            samples=req.samples,
            half_angle_deg=req.half_angle_deg,
        )
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "assignment": report.perm,
        "clean": report.is_clean,
        "conflicts": report.conflicts,
        "total_distance_m": round(report.total_distance_m, 3),
        "iterations": report.iterations,
    }


class DynamicsRequest(BaseModel):
    choreo: dict = Field(..., description="Choreography dataset")
    max_velocity_ms: float = Field(
        DEFAULT_DYNAMIC_LIMITS.max_velocity_ms, gt=0, description="Speed ceiling, m/s"
    )
    max_centripetal_ms2: float = Field(
        DEFAULT_DYNAMIC_LIMITS.max_centripetal_ms2, gt=0, description="Cornering ceiling, m/s^2"
    )
    min_separation_m: float = Field(
        DEFAULT_DYNAMIC_LIMITS.min_separation_m, gt=0, description="Minimum separation, m"
    )


@app.post("/api/validate-dynamics")
def api_validate_dynamics(req: DynamicsRequest):
    """
    Check a show is dynamically feasible: velocity, centripetal acceleration
    and pairwise separation.

    Distinct from /api/validate, which enforces the shared airframe envelope
    the GCS aborts on. These are the tighter choreography-quality bounds, and
    centripetal acceleration is a genuinely different quantity from the total
    acceleration /api/validate reports — it excludes the along-track component,
    so it isolates how hard the path is being bent.
    """
    limits = DynamicLimits(
        max_velocity_ms=req.max_velocity_ms,
        max_centripetal_ms2=req.max_centripetal_ms2,
        min_separation_m=req.min_separation_m,
    )
    try:
        return validate_dynamics(req.choreo, limits=limits)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/export")
def api_export(req: ExportRequest):
    fmt = req.format.lower()
    choreo = req.choreo
    rate_hz = req.rate_hz or 0.0

    # Bake the LED track server-side when one is supplied, so a .tara compiled
    # here matches what the Studio preview showed rather than depending on the
    # client having already written r/g/b/w onto every waypoint. Colours only
    # exist at waypoint times, so first add frames until every strobe has
    # samples in both its on- and off-phase - otherwise a strobe at or above
    # half the show's rate aliases into solid light.
    if req.led_track:
        try:
            factor = upsample_factor(choreo, led_sample_rate_hz(req.led_track))
            choreo = upsample_choreo(choreo, factor)
            rate_hz *= factor
            choreo = apply_track_to_choreo(choreo, req.led_track)
        except (ValueError, KeyError) as e:
            raise HTTPException(status_code=400, detail=f"Bad LED track: {e}")

    if fmt == "json":
        content = export_json(choreo)
        filename = "choreography.json"
        mime_type = "application/json"
    elif fmt == "csv":
        content = export_csv(choreo)
        filename = "choreography.csv"
        mime_type = "text/csv"
    elif fmt == "kml":
        content = export_kml(choreo, req.home_lat, req.home_lon)
        filename = "choreography.kml"
        mime_type = "application/vnd.google-earth.kml+xml"
    elif fmt == "tara":
        # Binary, so it travels base64-encoded; the client decodes before
        # writing the file. Flagged with `encoding` so a caller can't mistake
        # the payload for text.
        try:
            blob = export_tara(choreo, rate_hz)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Cannot compile .tara: {e}")
        return {
            "filename": "choreography.tara",
            "content": base64.b64encode(blob).decode("ascii"),
            "mime_type": "application/octet-stream",
            "encoding": "base64",
        }
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported export format: {req.format}")
        
    return {
        "filename": filename,
        "content": content,
        "mime_type": mime_type,
        "encoding": "utf-8",
    }

if __name__ == "__main__":
    import uvicorn
    import os
    host = os.environ.get("STUDIO_HOST", "127.0.0.1")
    port = int(os.environ.get("STUDIO_PORT", 8001))
    uvicorn.run(app, host=host, port=port)
