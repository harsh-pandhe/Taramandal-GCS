"""A show may name a horizontal site profile (``"site"``); Studio's backend keeps it and refuses an unknown one.

The profile itself (30 m / 13 pad columns by default, 60 m / 27 for ``scale400``) is defined once in
``tara_core.safety`` and consumed by the GCS pad grid; Studio only has to carry the name through export and
refuse a name no build knows, so a typo cannot produce a file the GCS then rejects at upload.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from tara_studio.server import app

client = TestClient(app)


def _show(**extra) -> dict:
    return {"drones": [{"id": 0, "waypoints": [
        {"time": 0.0, "x": 0.0, "y": 0.0, "z": -10.0, "yaw": 0.0},
        {"time": 5.0, "x": 0.0, "y": 0.0, "z": -10.0, "yaw": 0.0},
    ]}], **extra}


def test_export_keeps_a_named_site_in_the_json():
    res = client.post("/api/export", json={"choreo": _show(site="scale400"), "format": "json"})
    assert res.status_code == 200, res.text
    assert json.loads(res.json()["content"])["site"] == "scale400"


def test_export_without_a_site_is_unchanged():
    res = client.post("/api/export", json={"choreo": _show(), "format": "json"})
    assert res.status_code == 200, res.text
    assert "site" not in json.loads(res.json()["content"])


def test_export_refuses_an_unknown_site():
    res = client.post("/api/export", json={"choreo": _show(site="huge"), "format": "json"})
    assert res.status_code == 400
    assert "unknown site profile" in str(res.json()["detail"])


def test_validate_accepts_a_known_site_and_refuses_an_unknown_one():
    assert client.post("/api/validate", json=_show(site="scale400")).status_code == 200
    res = client.post("/api/validate", json=_show(site="huge"))
    assert res.status_code == 400
    assert "unknown site profile" in str(res.json()["detail"])
