"""
Taramandal Studio - FastAPI Server
Exposes API endpoints for generating, validating, and exporting drone shows.
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional

from tara_studio.generator import generate_circle, generate_helix, generate_heart, generate_cube, generate_spline
from tara_studio.validator import validate_choreography
from tara_studio.exporter import export_json, export_csv, export_kml
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
