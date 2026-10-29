"""Narrow local AI authoring (issue #133): one typed scene proposal at a time.

The model (local Ollama, schema-constrained JSON output) never touches
geometry, safety limits, or flight control. Its only job is to turn a short
natural-language prompt into a *closed-vocabulary* scene proposal - a shape
token picked from the same list the manual "Add Scene" UI offers
(`SHAPE_TOKENS`, mirroring `packages/tara-studio-web/src/engine/formations.js`'s
`SHAPE_LIBRARY`), plus a handful of bounded numeric fields. That proposal is
then applied the exact same way a human click would be: as a
`sceneCommands.add()` patch, going through Studio's existing deterministic
geometry generators (`formations.js`) and existing safety validator
(`/api/validate`) unchanged. This module never builds a trajectory and never
decides whether one is safe - it only ever produces, or refuses to produce,
one proposed *scene edit*.

A single shape *token* (not independent shapeType/shapePayload fields) is the
schema's one enum, deliberately: an early manual test asked the model for
shapeType+shapePayload separately and it returned a syntactically valid but
semantically wrong pair (shapeType "text" for shapePayload "heart", which is
actually a parametric shape). A flat token list removes that whole class of
mismatch structurally - the server maps a valid token straight to its correct
(shapeType, shapePayload) pair via `SHAPE_TOKENS`, so the model cannot express
an invalid combination at all.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field

#: Local Ollama's HTTP API. Override for a non-default install.
OLLAMA_URL = os.environ.get("TARA_OLLAMA_URL", "http://localhost:11434")
#: The model benchmarked and accepted for this role (Plan 4 S0, 2026-10-14):
#: qwen2.5:7b with schema-constrained output. Not re-benchmarked per issue.
OLLAMA_MODEL = os.environ.get("TARA_AI_MODEL", "qwen2.5:7b")
#: Generous enough for a 7B model's first token on a cold local GPU/CPU, short
#: enough that an operator isn't left waiting indefinitely on a wedged daemon.
OLLAMA_TIMEOUT_S = float(os.environ.get("TARA_AI_TIMEOUT_S", "30"))

#: Mirrors `SHAPE_LIBRARY` in formations.js by hand, same as every other
#: frontend/backend constant pair in this repo (e.g. `LAUNCH_PAD_SPACING_Y_M`).
#: token -> (shapeType, shapePayload).
SHAPE_TOKENS: dict[str, tuple[str, str]] = {
    "circle": ("parametric", "circle"),
    "grid": ("parametric", "grid"),
    "line": ("parametric", "line"),
    "heart": ("parametric", "heart"),
    "sphere": ("parametric", "sphere"),
    "vee": ("parametric", "vee"),
    **{f"text_{d}": ("text", str(d)) for d in range(10)},
    "svg_dandiya_sticks": ("svg", "dandiya_sticks"),
    "svg_diya_flame": ("svg", "diya_flame"),
}
#: Used in place of a real token when the operator attached their own SVG -
#: the model doesn't choose the shape in that case, only the other fields.
UPLOADED_SVG_TOKEN = "svg_uploaded"

_MIN_SCALE, _MAX_SCALE = 0.1, 10.0
_MIN_SECONDS, _MAX_SECONDS = 0.0, 60.0
_MAX_NAME_LEN = 60
_MAX_UNSUPPORTED_ITEMS = 10

#: One retry with the validation error appended, never more (issue #133:
#: "one bounded repair attempt; then show a clear rejection").
_MAX_ATTEMPTS = 2


@dataclass
class AIProposal:
    """Result of one `propose()` call. `status` is the single source of truth
    the API/UI switches on; the rest are populated only when relevant."""

    status: str  # "ok" | "schema_error" | "unavailable" | "timeout"
    name: str | None = None
    shape_type: str | None = None
    shape_payload: str | None = None
    scale: float | None = None
    hold_seconds: float | None = None
    transition_seconds: float | None = None
    unsupported: list[str] = field(default_factory=list)
    repaired: bool = False
    error: str | None = None


def _schema(allow_upload: bool) -> dict:
    tokens = [UPLOADED_SVG_TOKEN] if allow_upload else sorted(SHAPE_TOKENS)
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "shape": {"type": "string", "enum": tokens},
            "scale": {"type": "number"},
            "hold_seconds": {"type": "number"},
            "transition_seconds": {"type": "number"},
            "unsupported": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["name", "shape", "scale", "hold_seconds", "transition_seconds", "unsupported"],
    }


def _build_prompt(user_prompt: str, allow_upload: bool, repair_note: str | None) -> str:
    shape_help = (
        f'"shape" must be exactly "{UPLOADED_SVG_TOKEN}" - the operator already supplied the shape as an SVG file; '
        "you are only choosing the other fields and noting anything you cannot represent."
        if allow_upload
        else "\"shape\" must be exactly one of: " + ", ".join(sorted(SHAPE_TOKENS)) + ". "
        "Pick whichever is the closest real match to what's described; anything that doesn't map to one of "
        "these shapes, or to scale/hold_seconds/transition_seconds, goes in \"unsupported\" as a short phrase "
        "instead (for example colour, rotation, speed, or music-sync requests are not supported fields here)."
    )
    base = (
        "You are translating a drone light-show operator's request into a strictly typed scene description. "
        "You do not control safety limits, flight paths, or exact geometry - those are enforced separately. "
        f"{shape_help} "
        '"scale" is a relative size multiplier around 1.0. "hold_seconds" is how long the formation holds once '
        'reached; "transition_seconds" is how long the move into it takes. "name" is a short label for this scene. '
        "Reply with JSON only, matching the given schema exactly.\n\n"
        f"Operator request: {user_prompt.strip()}"
    )
    if repair_note:
        base += f"\n\nYour previous reply was rejected: {repair_note} Reply again, correcting this."
    return base


def _call_ollama(prompt: str, schema: dict) -> dict:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "format": schema,
        "stream": False,
        "options": {"temperature": 0.1},
    }
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_S) as resp:
        body = json.loads(resp.read())
    return json.loads(body["response"])


def _field_errors(parsed: dict, allow_upload: bool) -> list[str]:
    """Deterministic, independent of anything the model "intended" - exactly
    the same checks regardless of how the JSON got here, matching the
    issue's "schema/range checks ... are deterministic, independent of model
    output" acceptance bar."""
    errors = []
    if not isinstance(parsed.get("name"), str) or not parsed["name"].strip():
        errors.append("name must be a non-empty string")
    shape = parsed.get("shape")
    if allow_upload:
        if shape != UPLOADED_SVG_TOKEN:
            errors.append(f'shape must be "{UPLOADED_SVG_TOKEN}" when an SVG was uploaded')
    elif shape not in SHAPE_TOKENS:
        errors.append(f"shape must be one of {sorted(SHAPE_TOKENS)}")
    for key in ("scale", "hold_seconds", "transition_seconds"):
        v = parsed.get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            errors.append(f"{key} must be a number")
    unsupported = parsed.get("unsupported")
    if not isinstance(unsupported, list) or not all(isinstance(x, str) for x in unsupported):
        errors.append("unsupported must be a list of strings")
    return errors


def propose(user_prompt: str, svg_text: str | None = None) -> AIProposal:
    """One typed scene proposal for `user_prompt`.

    `svg_text` is the operator's own uploaded SVG (already sanitized by the
    frontend's `sanitizeSvg`): when given, the model never chooses the shape,
    only the remaining fields. Never raises - every failure mode (unreachable
    Ollama, a timeout, a model that won't produce valid JSON even after one
    repair attempt) is a distinct `status` on the returned `AIProposal`.
    """
    allow_upload = svg_text is not None
    schema = _schema(allow_upload)
    repair_note: str | None = None

    for attempt in range(_MAX_ATTEMPTS):
        prompt = _build_prompt(user_prompt, allow_upload, repair_note)
        try:
            parsed = _call_ollama(prompt, schema)
        except (TimeoutError, socket.timeout) as exc:
            return AIProposal(status="timeout", error=str(exc))
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                return AIProposal(status="timeout", error=str(exc))
            return AIProposal(status="unavailable", error=str(exc.reason))
        except (json.JSONDecodeError, KeyError, UnicodeDecodeError) as exc:
            repair_note = f"your reply was not valid JSON ({exc})."
            continue

        errors = _field_errors(parsed, allow_upload)
        if errors:
            repair_note = "; ".join(errors)
            continue

        shape_type, shape_payload = (
            ("svg", svg_text) if allow_upload else SHAPE_TOKENS[parsed["shape"]]
        )
        return AIProposal(
            status="ok",
            name=parsed["name"].strip()[:_MAX_NAME_LEN],
            shape_type=shape_type,
            shape_payload=shape_payload,
            scale=min(_MAX_SCALE, max(_MIN_SCALE, float(parsed["scale"]))),
            hold_seconds=min(_MAX_SECONDS, max(_MIN_SECONDS, float(parsed["hold_seconds"]))),
            transition_seconds=min(_MAX_SECONDS, max(_MIN_SECONDS, float(parsed["transition_seconds"]))),
            unsupported=[str(x) for x in parsed["unsupported"]][:_MAX_UNSUPPORTED_ITEMS],
            repaired=attempt > 0,
        )

    return AIProposal(status="schema_error", error=repair_note, repaired=True)
