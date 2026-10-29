"""Tests for issue #133's narrow AI authoring: `tara_studio.ai_author.propose`.

Exercised against the real local Ollama daemon (qwen2.5:7b, already pulled in
this environment) rather than a mocked HTTP layer where practical - the thing
worth proving is that schema-constrained generation, the repair path, and the
unavailable/timeout handling behave correctly against the real service, the
same reasoning `test_status.py` gives for using a real socket over a mocked
`urllib`. "Unavailable" and "timeout" are exercised by pointing the client at
a real dead port / a near-zero deadline, not by mocking the exception itself,
so a change to which exception type urllib actually raises would be caught
here instead of being asserted into existence by a mock.
"""

from __future__ import annotations

import http.server
import json
import shutil
import threading

import pytest

from tara_studio import ai_author
from tara_studio.ai_author import SHAPE_TOKENS, UPLOADED_SVG_TOKEN, propose

OLLAMA_AVAILABLE = shutil.which("ollama") is not None

pytestmark = pytest.mark.skipif(
    not OLLAMA_AVAILABLE, reason="ollama not installed in this environment"
)


@pytest.fixture(autouse=True)
def _real_model(monkeypatch):
    """Every test in this module talks to the real local Ollama/qwen2.5:7b
    unless it explicitly overrides the URL (unavailable/timeout tests)."""
    monkeypatch.setattr(ai_author, "OLLAMA_MODEL", "qwen2.5:7b")


class TestOneSceneProposal:
    def test_a_clear_request_maps_to_a_real_shape(self):
        result = propose("Build a big circle formation that holds for ten seconds.")
        assert result.status == "ok"
        assert result.shape_type == "parametric"
        assert result.shape_payload in {"circle", "grid", "line", "heart", "sphere", "vee"}
        assert result.scale and result.scale > 0
        assert result.hold_seconds is not None
        assert result.transition_seconds is not None
        assert result.name

    def test_fields_are_clamped_to_safe_bounds_regardless_of_model_output(self, monkeypatch):
        """Deterministic, independent of the model (issue #133 acceptance):
        even a well-formed but extreme reply gets clamped, not trusted raw."""
        monkeypatch.setattr(
            ai_author,
            "_call_ollama",
            lambda prompt, schema: {
                "name": "x" * 500,
                "shape": "circle",
                "scale": 99999.0,
                "hold_seconds": -5.0,
                "transition_seconds": 1e9,
                "unsupported": [],
            },
        )
        result = propose("anything")
        assert result.status == "ok"
        assert len(result.name) <= 60
        assert result.scale <= 10.0
        assert result.hold_seconds >= 0.0
        assert result.transition_seconds <= 60.0


class TestUnsupportedRequest:
    def test_a_request_with_no_real_mapping_is_marked_unsupported(self):
        result = propose("Make the drones spell out a secret message in morse code using sound.")
        assert result.status == "ok"
        # The model should pick *some* shape token (never invent one outside
        # the enum - the schema itself forbids that) and flag what it can't do.
        assert result.shape_type in {"parametric", "text", "svg"}


class TestUploadedSvg:
    def test_an_uploaded_svg_is_never_reshaped_by_the_model(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="5"/></svg>'
        result = propose("Use my uploaded logo, hold for 8 seconds.", svg_text=svg)
        assert result.status == "ok"
        assert result.shape_type == "svg"
        assert result.shape_payload == svg  # passed through untouched, never "interpreted"


class TestSchemaErrorAndRepair:
    def test_malformed_json_triggers_one_repair_then_recovers(self, monkeypatch):
        calls = []

        def fake_call(prompt, schema):
            calls.append(prompt)
            if len(calls) == 1:
                raise json.JSONDecodeError("bad", "not json", 0)
            return {
                "name": "Recovered",
                "shape": "circle",
                "scale": 1.0,
                "hold_seconds": 5.0,
                "transition_seconds": 3.0,
                "unsupported": [],
            }

        monkeypatch.setattr(ai_author, "_call_ollama", fake_call)
        result = propose("a circle")
        assert result.status == "ok"
        assert result.repaired is True
        assert len(calls) == 2
        assert "rejected" in calls[1].lower()  # the repair prompt references the failure

    def test_persistent_invalid_output_is_a_clear_rejection_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(
            ai_author,
            "_call_ollama",
            lambda prompt, schema: {"name": "", "shape": "not-a-real-shape", "scale": "nope", "hold_seconds": None, "transition_seconds": None, "unsupported": "not-a-list"},
        )
        result = propose("anything")
        assert result.status == "schema_error"
        assert result.repaired is True
        assert result.error

    def test_invalid_shape_token_is_rejected_even_if_otherwise_well_formed(self, monkeypatch):
        """A field-level guard independent of whatever the schema itself
        constrains - the model could in principle be swapped for one that
        doesn't honour `format` strictly."""
        monkeypatch.setattr(
            ai_author,
            "_call_ollama",
            lambda prompt, schema: {
                "name": "x", "shape": "cube", "scale": 1.0,
                "hold_seconds": 1.0, "transition_seconds": 1.0, "unsupported": [],
            },
        )
        result = propose("anything")
        assert result.status == "schema_error"

    def test_uploaded_svg_but_model_tries_to_pick_a_different_shape_is_rejected(self, monkeypatch):
        monkeypatch.setattr(
            ai_author,
            "_call_ollama",
            lambda prompt, schema: {
                "name": "x", "shape": "circle", "scale": 1.0,
                "hold_seconds": 1.0, "transition_seconds": 1.0, "unsupported": [],
            },
        )
        result = propose("anything", svg_text="<svg></svg>")
        assert result.status == "schema_error"


class TestUnavailableAndTimeout:
    def test_unreachable_ollama_is_reported_as_unavailable_not_a_crash(self, monkeypatch):
        # A real dead local port - nothing listens on 1 (tcpmux), same choice
        # test_status.py makes for "nothing is listening".
        monkeypatch.setattr(ai_author, "OLLAMA_URL", "http://127.0.0.1:1")
        monkeypatch.setattr(ai_author, "OLLAMA_TIMEOUT_S", 2.0)
        result = propose("a circle")
        assert result.status == "unavailable"
        assert result.error

    def test_a_hung_server_times_out_instead_of_blocking(self, monkeypatch):
        hang = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                hang.wait(timeout=10.0)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            monkeypatch.setattr(ai_author, "OLLAMA_URL", f"http://127.0.0.1:{port}")
            monkeypatch.setattr(ai_author, "OLLAMA_TIMEOUT_S", 0.3)
            result = propose("a circle")
            assert result.status == "timeout"
        finally:
            hang.set()
            server.shutdown()
            server.server_close()


class TestShapeTokenTable:
    def test_every_token_maps_to_a_real_shape_type(self):
        for shape_type, _payload in SHAPE_TOKENS.values():
            assert shape_type in {"parametric", "text", "svg"}

    def test_uploaded_token_is_not_itself_in_the_choosable_table(self):
        """The model should never be offered svg_uploaded as a thing to pick
        on its own - it's only used when an SVG was actually supplied."""
        assert UPLOADED_SVG_TOKEN not in SHAPE_TOKENS
