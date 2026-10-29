"""Tests for POST /api/ai/propose (issue #133) - the HTTP surface over
`tara_studio.ai_author.propose`. The module's own behavior (schema
enforcement, repair, unavailable/timeout) is covered by test_ai_author.py;
this file only checks the endpoint's own contract: always 200 with a
`status` field, request validation, and that it forwards to `ai_propose`
correctly.
"""

from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient

from tara_studio import server
from tara_studio.ai_author import AIProposal
from tara_studio.server import app

client = TestClient(app)

OLLAMA_AVAILABLE = shutil.which("ollama") is not None


def test_a_real_request_returns_200_with_a_typed_draft():
    if not OLLAMA_AVAILABLE:
        pytest.skip("ollama not installed in this environment")
    res = client.post("/api/ai/propose", json={"prompt": "A circle that holds for 5 seconds."})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["shape_type"] in {"parametric", "text", "svg"}


def test_an_empty_prompt_is_rejected_before_it_reaches_the_model():
    res = client.post("/api/ai/propose", json={"prompt": ""})
    assert res.status_code == 422


def test_an_oversized_prompt_is_rejected():
    res = client.post("/api/ai/propose", json={"prompt": "x" * 3000})
    assert res.status_code == 422


def test_svg_is_optional():
    """A missing `svg` must not 422 - most proposals have none."""
    res = client.post("/api/ai/propose", json={"prompt": "a circle"})
    assert res.status_code != 422


def test_endpoint_forwards_the_uploaded_svg_unchanged(monkeypatch):
    seen = {}

    def fake_propose(prompt, svg_text=None):
        seen["prompt"] = prompt
        seen["svg_text"] = svg_text
        return AIProposal(status="ok", name="n", shape_type="svg", shape_payload=svg_text, scale=1.0, hold_seconds=1.0, transition_seconds=1.0)

    monkeypatch.setattr(server, "ai_propose", fake_propose)
    svg = "<svg><circle/></svg>"
    res = client.post("/api/ai/propose", json={"prompt": "use my logo", "svg": svg})
    assert res.status_code == 200
    assert seen["svg_text"] == svg
    assert res.json()["shape_payload"] == svg


def test_every_status_the_module_can_return_serializes_cleanly(monkeypatch):
    """The endpoint must not special-case any status - a 200 with the status
    field either way, matching the module's "never raises" contract."""
    for status in ("ok", "schema_error", "unavailable", "timeout"):
        monkeypatch.setattr(server, "ai_propose", lambda prompt, svg_text=None, s=status: AIProposal(status=s))
        res = client.post("/api/ai/propose", json={"prompt": "x"})
        assert res.status_code == 200
        assert res.json()["status"] == status
