"""Local scene-edit planner, slice 1 (issue #94): one sentence -> up to 3 ops.

The model (local Ollama, schema-constrained JSON) is shown a compact summary
of the current scenes and answers with at most three *ops*, each either a
``patch`` of one existing scene (referred to by its 1-based NUMBER, never an
id) or an ``add`` of a new scene. It only drafts. The browser compiles the ops
into one ``scene/batch`` and the existing proposal flow (dry-run, safety check,
explicit Apply, one undo step) decides whether anything happens. Nothing here
builds geometry, touches safety limits, fleet or flight control.

Contract points, all deliberate:

* Exactly ONE model call per request, no internal retry. The "one bounded
  repair" budget belongs to the browser, which calls again with
  ``repair_note`` when its own check refuses the draft. Total <= 2 calls.
* Values are never clamped or range-checked here. A scale of 9 is returned as
  9; the browser's ``batchRefusal`` is the authority and refuses it. (This is
  different from ``ai_author``, which clamps.)
* Never raises: every failure is a ``status``.
"""

from __future__ import annotations

import json
import socket
import urllib.error
from dataclasses import dataclass, field

from tara_studio.ai_author import (  # reuse, do not copy
    OLLAMA_MODEL,
    OLLAMA_TIMEOUT_S,
    OLLAMA_URL,
    SHAPE_TOKENS,
    _call_ollama,
)

__all__ = ["OLLAMA_MODEL", "OLLAMA_TIMEOUT_S", "OLLAMA_URL", "PlanResult", "plan", "MAX_OPS"]

MAX_OPS = 3
_MAX_UNSUPPORTED_ITEMS = 10
_NUMERIC = ("scale", "hold_seconds", "transition_seconds")
_PATCH_FIELDS = ("name", "shape", *_NUMERIC)


@dataclass
class PlanResult:
    """`status` is the single source of truth: ok | schema_error | unavailable | timeout."""

    status: str
    ops: list[dict] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    error: str | None = None


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "ops": {
                "type": "array",
                "maxItems": MAX_OPS,
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {"type": "string", "enum": ["patch", "add"]},
                        "scene": {"type": "integer"},
                        "name": {"type": "string"},
                        "shape": {"type": "string", "enum": sorted(SHAPE_TOKENS)},
                        "scale": {"type": "number"},
                        "hold_seconds": {"type": "number"},
                        "transition_seconds": {"type": "number"},
                    },
                    "required": ["op", "scene"],
                },
            },
            "unsupported": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["ops", "unsupported"],
    }


def _scene_line(s: dict) -> str:
    # The name is operator/file-controlled text going into a prompt: JSON-encode it so
    # quotes and newlines cannot fake extra scene lines or instructions. (The schema and the
    # checks below stay the only contract either way.)
    return (
        f'{s["number"]}. {json.dumps(str(s["name"]))} shape={json.dumps(str(s["shape"]))} scale={s["scale"]} '
        f'hold_seconds={s["hold_seconds"]} transition_seconds={s["transition_seconds"]}'
    )


def _build_prompt(user_prompt: str, scenes: list[dict], repair_note: str | None) -> str:
    listing = "\n".join(_scene_line(s) for s in scenes)
    base = (
        "You are drafting edits to a drone light-show for an operator. You may only edit a scene's "
        "shape, scale, hold_seconds, transition_seconds and name, or add a new scene. Scenes are "
        "referred to by their number in the list below, never by any other identifier. "
        f'Reply with at most {MAX_OPS} ops. An op is {{"op":"patch","scene":N,...fields to change}} '
        'or {"op":"add","scene":0,"shape":...,...fields}. '
        '"shape" must be exactly one of: ' + ", ".join(sorted(SHAPE_TOKENS)) + ". "
        "Anything else the operator asks for - safety limits, fleet or drone counts, music, colours, "
        "show length, flight or vehicle commands, moving/deleting/duplicating scenes - cannot be "
        'done here: put it in "unsupported" as a short phrase and do not invent an op for it. '
        "Reply with JSON only, matching the given schema exactly.\n\n"
        f"Current scenes:\n{listing}\n\n"
        f"Operator request: {user_prompt.strip()}"
    )
    if repair_note:
        base += f"\n\nYour previous reply was rejected: {repair_note} Reply again, correcting this."
    return base


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _op_errors(raw, scenes_by_number: dict[int, dict]) -> list[str]:
    errors: list[str] = []
    if not isinstance(raw, dict):
        return ["each op must be an object"]
    op = raw.get("op")
    if op not in ("patch", "add"):
        errors.append('op must be "patch" or "add"')
        return errors
    for key in _NUMERIC:
        if key in raw and not _is_number(raw[key]):
            errors.append(f"{key} must be a number")
    if "name" in raw and (not isinstance(raw["name"], str) or not raw["name"].strip()):
        errors.append("name must be a non-empty string")
    if "shape" in raw and raw["shape"] not in SHAPE_TOKENS:
        errors.append(f"shape must be one of {sorted(SHAPE_TOKENS)}")

    if op == "patch":
        n = raw.get("scene")
        if not isinstance(n, int) or isinstance(n, bool) or n not in scenes_by_number:
            errors.append(f"patch scene must be a number from 1 to {len(scenes_by_number)}")
            return errors
        given = [k for k in _PATCH_FIELDS if k in raw]
        if not given:
            errors.append(f"patch of scene {n} changes no field")
        elif not errors:
            cur = scenes_by_number[n]
            if all(raw[k] == cur.get(k) for k in given):
                errors.append(f"patch of scene {n} changes nothing (those values are already set)")
    else:
        if raw.get("scene") not in (0, None):
            errors.append("add must use scene 0")
        if "shape" not in raw:
            errors.append("add needs a shape")
    return errors


def plan(user_prompt: str, scenes: list[dict], repair_note: str | None = None) -> PlanResult:
    """One planning call. Never raises and never retries; see the module docstring."""
    prompt = _build_prompt(user_prompt, scenes, repair_note)
    try:
        parsed = _call_ollama(prompt, _schema())
    except (TimeoutError, socket.timeout) as exc:
        return PlanResult(status="timeout", error=str(exc))
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return PlanResult(status="timeout", error=str(exc))
        return PlanResult(status="unavailable", error=str(exc.reason))
    except OSError as exc:  # connection refused / reset before urllib wraps it
        return PlanResult(status="unavailable", error=str(exc))
    except (json.JSONDecodeError, KeyError, UnicodeDecodeError, TypeError) as exc:
        return PlanResult(status="schema_error", error=f"your reply was not valid JSON ({exc}).")

    if not isinstance(parsed, dict):
        return PlanResult(status="schema_error", error="your reply must be a JSON object.")

    raw_ops = parsed.get("ops")
    unsupported = parsed.get("unsupported")
    if not isinstance(raw_ops, list):
        return PlanResult(status="schema_error", error="ops must be a list.")
    if not isinstance(unsupported, list) or not all(isinstance(x, str) for x in unsupported):
        return PlanResult(status="schema_error", error="unsupported must be a list of strings.")
    unsupported = unsupported[:_MAX_UNSUPPORTED_ITEMS]
    if len(raw_ops) > MAX_OPS:
        return PlanResult(status="schema_error", error=f"at most {MAX_OPS} ops are allowed, got {len(raw_ops)}.")
    if not raw_ops and not unsupported:
        return PlanResult(
            status="schema_error",
            error="ops is empty and nothing is listed as unsupported; return ops, or say in unsupported why none apply.",
        )

    by_number = {s["number"]: s for s in scenes}
    errors: list[str] = []
    for i, raw in enumerate(raw_ops, start=1):
        errors += [f"op {i}: {e}" for e in _op_errors(raw, by_number)]
    if errors:
        return PlanResult(status="schema_error", error="; ".join(errors))

    ops = []
    for raw in raw_ops:
        out: dict = {"op": raw["op"], "scene": 0 if raw["op"] == "add" else raw["scene"]}
        if "name" in raw:
            out["name"] = raw["name"].strip()
        if "shape" in raw:
            out["shape_type"], out["shape_payload"] = SHAPE_TOKENS[raw["shape"]]
        for key in _NUMERIC:
            if key in raw:
                out[key] = float(raw[key])  # unclamped on purpose
        ops.append(out)
    return PlanResult(status="ok", ops=ops, unsupported=[str(x) for x in unsupported])
