"""Issue #148: /api/export rejects a show above the 120 m ceiling or below ground.

Unlike feasibility certification (issue #60, see test_export_feasibility_gate.py's
module docstring for why that one does NOT block), the altitude ceiling is an
absolute physical/airspace limit, not a choreography-quality bound, and the
client-side formation builder and morph planner have always refused to produce
anything above it -- so unlike jerk, no real generator output is expected to be
affected by hard-blocking this one. Before this gate existed, nothing on the
backend enforced it at all: a client that bypassed the Studio UI, or a show
built before the frontend gates existed, could export and fly arbitrarily high.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tara_studio.generator import generate_circle
from tara_studio.server import app

client = TestClient(app)


def _choreo_at(z: float) -> dict:
    return {"drones": [{"id": 0, "waypoints": [
        {"time": 0.0, "x": 0.0, "y": 0.0, "z": z, "yaw": 0.0},
        {"time": 5.0, "x": 0.0, "y": 0.0, "z": z, "yaw": 0.0},
    ]}]}


def test_export_rejects_the_148_repro():
    res = client.post("/api/export", json={"choreo": _choreo_at(-150.0), "format": "json"})
    assert res.status_code == 400
    assert "Altitude violation" in str(res.json()["detail"]["errors"])


def test_export_rejects_exactly_above_the_ceiling():
    res = client.post("/api/export", json={"choreo": _choreo_at(-120.01), "format": "json"})
    assert res.status_code == 400


def test_export_accepts_exactly_at_the_ceiling():
    res = client.post("/api/export", json={"choreo": _choreo_at(-120.00), "format": "json"})
    assert res.status_code == 200


def test_export_rejects_ground_penetration():
    res = client.post("/api/export", json={"choreo": _choreo_at(1.0), "format": "json"})
    assert res.status_code == 400
    assert "Ground violation" in str(res.json()["detail"]["errors"])


def test_export_still_succeeds_for_a_real_generated_shape():
    # The explicit regression #148's acceptance criteria asks for: this gate
    # must not reject ordinary Studio output, only genuine ceiling breaches.
    choreo = generate_circle(5, 10.0, 2.0)
    res = client.post("/api/export", json={"choreo": choreo, "format": "json"})
    assert res.status_code == 200


def test_the_altitude_gate_applies_to_every_export_format():
    # Picked because they take different code paths inside api_export (csv
    # has no schema step at all, kml takes home coordinates); the altitude
    # check runs before any of that, so all three must reject identically.
    for fmt, extra in (("json", {}), ("csv", {}), ("kml", {"home_lat": 12.9, "home_lon": 77.6})):
        res = client.post("/api/export", json={"choreo": _choreo_at(-150.0), "format": fmt, **extra})
        assert res.status_code == 400, f"format={fmt}"
