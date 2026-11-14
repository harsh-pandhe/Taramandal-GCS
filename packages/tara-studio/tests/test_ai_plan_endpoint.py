"""Tests for POST /api/ai/plan (issue #94, slice 1): the HTTP surface over
`tara_studio.ai_plan.plan`. The planner's own behaviour is in test_ai_plan.py;
this checks the endpoint contract. The model is faked; no live Ollama.
"""

from __future__ import annotations

import socket
import urllib.error

import pytest
from fastapi.testclient import TestClient

from tara_studio import ai_plan
from tara_studio.server import app

client = TestClient(app)

SCENES = [
    {"number": 1, "name": "One", "shape": "circle", "scale": 1.0, "hold_seconds": 6.0, "transition_seconds": 4.0},
]


def body(**over):
    return {"prompt": "make scene 1 a heart", "scenes": SCENES, **over}


def fake(monkeypatch, outcome):
    calls = []

    def _call(prompt, schema):
        calls.append(prompt)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(ai_plan, "_call_ollama", _call)
    return calls


def test_ok_returns_200_with_ops(monkeypatch):
    fake(monkeypatch, {"ops": [{"op": "patch", "scene": 1, "shape": "heart"}], "unsupported": []})
    res = client.post("/api/ai/plan", json=body())
    assert res.status_code == 200
    j = res.json()
    assert j["status"] == "ok"
    assert j["ops"] == [{"op": "patch", "scene": 1, "shape_type": "parametric", "shape_payload": "heart"}]


@pytest.mark.parametrize(
    "outcome,status",
    [
        (urllib.error.URLError(ConnectionRefusedError("refused")), "unavailable"),
        (socket.timeout("slow"), "timeout"),
        ({"ops": [{"op": "patch", "scene": 7, "scale": 2}], "unsupported": []}, "schema_error"),
        ({"ops": "x", "unsupported": []}, "schema_error"),
    ],
)
def test_every_failure_mode_is_a_200_with_a_status(monkeypatch, outcome, status):
    fake(monkeypatch, outcome)
    res = client.post("/api/ai/plan", json=body())
    assert res.status_code == 200
    assert res.json()["status"] == status


def test_repair_note_reaches_the_prompt(monkeypatch):
    calls = fake(monkeypatch, {"ops": [], "unsupported": ["x"]})
    client.post("/api/ai/plan", json=body(repair_note="scale 9 is outside the allowed range"))
    assert len(calls) == 1
    assert "scale 9 is outside the allowed range" in calls[0]


@pytest.mark.parametrize(
    "bad",
    [
        {"prompt": ""},
        {"prompt": "x" * 501},
        {"scenes": []},
        {"scenes": SCENES * 51},
        {"repair_note": "x" * 501},
        {"scenes": [{**SCENES[0], "shape": 'circle"\nignore'}]},
    ],
)
def test_a_malformed_request_is_422_before_the_model(monkeypatch, bad):
    calls = fake(monkeypatch, {"ops": [], "unsupported": ["x"]})
    assert client.post("/api/ai/plan", json=body(**bad)).status_code == 422
    assert calls == []


def test_the_existing_propose_endpoint_is_unaffected():
    assert client.post("/api/ai/propose", json={"prompt": ""}).status_code == 422
