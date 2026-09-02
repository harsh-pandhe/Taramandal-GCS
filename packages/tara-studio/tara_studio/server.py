"""
Taramandal Studio - FastAPI Server
Exposes API endpoints for generating, validating, and exporting drone shows.
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional

from tara_studio.generator import generate_circle, generate_helix, generate_heart, generate_cube, generate_spline
from tara_studio.validator import (
    DEFAULT_DYNAMIC_LIMITS,
    DynamicLimits,
    validate_choreography,
    validate_dynamics,
)
from tara_studio.exporter import export_json, export_csv, export_kml
from tara_studio.optimizer import (
    DEFAULT_MORPH_SAMPLES,
    DEFAULT_PENALTY_WEIGHT,
    DOWNWASH_HALF_ANGLE_DEG,
    aerodynamic_assignment,
)
from tara_core import MAX_FLEET_SIZE
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
    format: str = Field("json", description="Export file format: json, csv, kml")
    home_lat: Optional[float] = Field(12.9716, description="Home latitude for KML maps")
    home_lon: Optional[float] = Field(77.5946, description="Home longitude for KML maps")

@app.get("/status")
def status():
    return {"status": "ONLINE", "engine": "Taramandal-Studio-v1"}

@app.post("/api/generate")
def api_generate(req: GenerateRequest):
    shape = req.shape.lower()
    
    if shape == "circle":
        choreo = generate_circle(req.num_drones, req.duration, req.rate, req.spawn_offsets, req.target_heights)
    elif shape == "helix":
        choreo = generate_helix(req.num_drones, req.duration, req.rate, req.spawn_offsets, req.target_heights)
    elif shape == "heart":
        choreo = generate_heart(req.num_drones, req.duration, req.rate, req.spawn_offsets, req.target_heights)
    elif shape == "cube":
        choreo = generate_cube(req.num_drones, req.duration, req.rate, req.spawn_offsets, req.target_heights)
    elif shape == "spline":
        choreo = generate_spline(req.num_drones, req.duration, req.rate, req.spawn_offsets, req.target_heights)
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported shape format: {req.shape}")
        
    # Perform safety validation check automatically
    validation = validate_choreography(choreo)
    
    return {
        "choreo": choreo,
        "validation": validation
    }

@app.post("/api/validate")
def api_validate(choreo: dict):
    return validate_choreography(choreo)

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
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported export format: {req.format}")
        
    return {
        "filename": filename,
        "content": content,
        "mime_type": mime_type
    }

if __name__ == "__main__":
    import uvicorn
    import os
    host = os.environ.get("STUDIO_HOST", "127.0.0.1")
    port = int(os.environ.get("STUDIO_PORT", 8001))
    uvicorn.run(app, host=host, port=port)
