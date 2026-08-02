"""
Taramandal Studio - FastAPI Server
Exposes API endpoints for generating, validating, and exporting drone shows.
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional

from studio.generator import generate_circle, generate_helix, generate_heart, generate_cube, generate_spline
from studio.validator import validate_choreography
from studio.exporter import export_json, export_csv, export_kml
from studio.assignment import optimal_assignment

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
    num_drones: int = Field(3, ge=1, le=10, description="Number of drones")
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
