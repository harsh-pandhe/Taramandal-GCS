"""Local scene-edit planner (issue #94): one sentence -> up to 3 ops.

The model (local Ollama, schema-constrained JSON) is shown a compact summary
of the current scenes and answers with at most three *ops*: a ``patch`` of one
existing scene (referred to by its 1-based NUMBER, never an id), an ``add`` of a
new scene, or a ``move``, ``delete`` or ``duplicate`` of an existing one. It only
drafts. The browser compiles the ops
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
* Two kinds of bad draft are answered here, finally, instead of being sent back to
  the model for a repair: a scene number the show does not have, and a draft that
  changes nothing. Both came up with the real qwen2.5:7b: asked to edit "scene 9" of
  a 3-scene show it answered with an ``add`` and, told "add must use scene 0", added
  a scene nobody asked for; asked to "turn the drones red" it returned no-op patches
  of every scene. A repair can only make the model guess, so the answer is "no ops"
  with the reason in ``unsupported``, which the browser shows as "not supported".
* Two deterministic guards cover rules the 7B model ignored in its prompt:
  - Show-length guard: a request to change the total show length/duration that names
    no scene is answered before the model is called (ZERO model calls): no ops and a
    "not supported" reason. Asked for a target length, the model rewrote the timings.
  - Name fallback: if the model omits the optional ``name`` of an ``add`` but the
    prompt has an explicit "called/named/titled X", X is used (first nameless add
    only, never over a model-supplied name, never truncated past 60 characters).
* move / delete / duplicate rules, checked here and not left to the model: a move is the
  only op of its draft; at most one delete; a delete or move (and a move's before/after
  target) only counts if the operator wrote that number as a scene reference ("scene 2",
  "#2"), otherwise the answer is final ("needs a scene number") with no repair; a scene
  or target the show does not have is the existing final "does not exist" answer.
"""

from __future__ import annotations

import json
import re
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
OPS = ("patch", "add", "move", "delete", "duplicate")
_SCENE_OPS = ("move", "delete", "duplicate")  # act on an existing scene, never edit its fields
_POSITIONS = ("before", "after", "first", "last")
_MAX_NAME_LEN = 60  # mirrors MAX_SCENE_NAME_LENGTH in the web app's sceneLimits.js

SHOW_LENGTH_UNSUPPORTED = (
    "changing the total show length is not supported by the planner; "
    "edit scene holds and transitions instead"
)

# --- Show-length guard (no model call) ---------------------------------------
# The planner cannot hit a target total length: asked to, the 7B model rewrites
# hold/transition values of every scene. So a request about the show's overall
# length is answered here, finally, before any model call. A request that names a
# scene ("set hold of scene 1 to 90 seconds") is an ordinary edit and is not caught.
# Every quantifier is bounded and the alternatives do not overlap, so there is no
# catastrophic backtracking on the 500-character prompt limit.
# A duration: 2:00, 90 s, 2 minutes, or a spelled-out number ("two minutes"). A number word needs a
# space and a full unit, so "show as a heart" cannot read as "show a(s)".
_NUM_WORD = r"(?:a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|sixty)"
_DUR = (
    r"(?:\d{1,4}:\d{2}"
    r"|\d{1,4}(?:\.\d{1,2})?\s*(?:minutes?|mins?|seconds?|secs?|s)"
    rf"|{_NUM_WORD}\s+(?:minutes?|mins?|seconds?|secs?))"
)
_SHOW_LENGTH = re.compile(
    r"\b(?:show|total|overall|whole|entire)\s+(?:length|duration|runtime)\b"
    r"|\b(?:shorten|lengthen|extend|stretch|shrink)\s+(?:the\s+|this\s+|my\s+)?(?:(?:whole|entire)\s+)?show\b"
    r"|\b(?:shorter|longer)\s+show\b|\bshow\s+(?:shorter|longer)\b"
    rf"|\bshow\s+(?:(?:should\s+be|to\s+be|be|is|to|for)\s+)?(?:(?:about|around)\s+)?{_DUR}\b"
    rf"|\bit\s+(?:(?:about|around)\s+)?{_DUR}\s+long\b",
    re.IGNORECASE,
)
_NAMES_A_SCENE = re.compile(r"\bscenes?\s*#?\s*\d", re.IGNORECASE)


def asks_for_show_length(prompt: str) -> bool:
    return bool(_SHOW_LENGTH.search(prompt)) and not _NAMES_A_SCENE.search(prompt)


# --- Name fallback for `add` ---------------------------------------------------
# The model often leaves out the optional "name" of an added scene even when the
# operator gave one. Only an explicit naming phrase attached to a scene is used:
# "scene called/named/titled" followed by a quoted string, or by words up to punctuation
# or a stop word. "after the one called Opening" is not a name for the new scene.
_NAME_CUE = re.compile(
    r"\bscenes?\s+(?:called|named|titled)\s+"
    r"(?:\"([^\"]*)\"|'([^']*)'|“([^”]*)”|‘([^’]*)’|(.*))",
    re.IGNORECASE | re.DOTALL,
)
_NAME_END = re.compile(r"[,.;:!?\n]|\b(?:with|that|which|and|then)\b", re.IGNORECASE)
_QUOTES = "\"'“”‘’"


def name_from_prompt(prompt: str) -> str | None:
    """The scene name the operator spelled out, or None. Never truncates."""
    m = _NAME_CUE.search(prompt)
    if not m:
        return None
    quoted = next((g for g in m.groups()[:4] if g is not None), None)
    if quoted is not None:
        name = quoted
    else:
        tail = m.group(5)
        end = _NAME_END.search(tail)
        name = tail[: end.start()] if end else tail
    name = name.strip().strip(_QUOTES).strip()
    if not name or len(name) > _MAX_NAME_LEN:
        return None
    return name


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
                        "op": {"type": "string", "enum": list(OPS)},
                        "scene": {"type": "integer"},
                        "position": {"type": "string", "enum": list(_POSITIONS)},
                        "target": {"type": "integer"},
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
        "shape, scale, hold_seconds, transition_seconds and name, add a new scene, or move, delete or "
        "duplicate an existing scene. Scenes are "
        "referred to by their number in the list below, never by any other identifier. "
        f'Reply with at most {MAX_OPS} ops. An op is {{"op":"patch","scene":N,...fields to change}} '
        'or {"op":"add","scene":0,"shape":...,...fields}. '
        'To reorder: {"op":"move","scene":N,"position":"before" or "after","target":M} puts scene N '
        'before/after scene M, and {"op":"move","scene":N,"position":"first" or "last"} (no target) puts it at the '
        'start/end. {"op":"delete","scene":N} removes a scene; {"op":"duplicate","scene":N} copies it. '
        "delete, duplicate and move carry no other fields. "
        '"shape" must be exactly one of: ' + ", ".join(sorted(SHAPE_TOKENS)) + ". "
        "Anything else the operator asks for - safety limits, fleet or drone counts, music, colours, "
        "show length, flight or vehicle commands - cannot be "
        'done here: put it in "unsupported" as a short phrase and do not invent an op for it. '
        "Rules: do exactly what the operator asked and nothing more. If the operator names a scene number that is "
        "not in the list, do not guess another scene and do not add one: return no ops and say in unsupported "
        'that scene N does not exist. Do not change hold_seconds or transition_seconds unless the operator '
        "names a scene and a time for it, and never stretch or shrink scenes to reach a total show length. "
        'When the operator gives a name for a new scene, put it in "name". '
        "A move must be the only op in your reply. Delete at most one scene. Only delete or move a scene when "
        "the operator wrote its number as 'scene N' (for example 'scene 2' or '#2'), and for a move before/after "
        "also the target as 'scene M'; if they did not, or they ask you to pick scenes yourself (for example "
        '"remove the boring scenes"), return no ops and put "needs a scene number" in unsupported. '
        '"unsupported" lists only things the operator asked for that you did not do; if you did everything '
        "that was asked it must be an empty list. Never describe your edits in it and never copy a rejection "
        "message into it. "
        "Reply with JSON only, matching the given schema exactly.\n\n"
        f"Current scenes:\n{listing}\n\n"
        f"Operator request: {user_prompt.strip()}"
    )
    if repair_note:
        base += f"\n\nYour previous reply was rejected: {repair_note} Reply again, correcting this."
    return base


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


SCENE = "scene"  # error kind: the op names a scene number the show does not have
NOOP = "noop"  # error kind: the op changes nothing
NUMBER = "number"  # error kind: delete/move without the scene number written in the operator's prompt


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# A scene number only counts when it is written as a scene reference: "scene 2", "Scene2",
# "scenes 2", "#2", "scene #2". A bare number ("hold 3 seconds", "after 3") does not. The
# alternatives are anchored on a literal word or "#" and each \s* is followed by a different
# token, so matching is linear in the (at most 500 character) prompt.
_SCENE_REF = re.compile(r"(?:\bscenes?\s*(?:#\s*)?|#\s*)(\d+)\b", re.IGNORECASE | re.ASCII)


def prompt_numbers(prompt: str) -> set[int]:
    """Scene numbers the operator wrote as a reference; the 12 in "scene 12" is not a 2."""
    return {int(m) for m in _SCENE_REF.findall(prompt)}


# A written number must also belong to the right word. "move scene 1 after scene 3 and make scene 2 a
# heart" wrote 1, 3 and 2, and a real qwen2.5:7b draft moved scene 2: the 2 was written, but for the heart.
# So the scene being deleted must follow a delete verb, the scene being moved a move verb, and a move's
# target "before"/"after". Same shape as _SCENE_REF, each pattern anchored on a literal word.
_REF = r"(?:the\s+)?(?:scenes?\s*(?:#\s*)?|#\s*)(\d+)\b"
_DELETE_REF = re.compile(rf"\b(?:delete|remove|drop|get\s+rid\s+of)\s+{_REF}", re.IGNORECASE | re.ASCII)
_MOVE_REF = re.compile(rf"\b(?:move|put|send|shift|relocate|reorder)\s+{_REF}", re.IGNORECASE | re.ASCII)
_TARGET_REF = re.compile(rf"\b(?:before|after)\s+{_REF}", re.IGNORECASE | re.ASCII)
_END_CUE = re.compile(r"\b(?:end|last|bottom|back)\b", re.IGNORECASE | re.ASCII)
_START_CUE = re.compile(r"\b(?:start|beginning|first|top|front)\b", re.IGNORECASE | re.ASCII)


def prompt_refs(prompt: str) -> dict[str, set[int]]:
    """Scene numbers the operator wrote for each role: ``delete`` and ``move`` (the scene acted on) and
    ``target`` (the scene a move is before/after)."""
    return {
        "delete": {int(m) for m in _DELETE_REF.findall(prompt)},
        "move": {int(m) for m in _MOVE_REF.findall(prompt)},
        "target": {int(m) for m in _TARGET_REF.findall(prompt)},
    }


def _normalise_moves(raw_ops: list, prompt: str, count: int, refs: dict[str, set[int]]) -> list:
    """The model turns "move scene 2 to the end" into "after scene 3", a target the operator never wrote.
    "After the last scene" is the same place as "the end", so when the sentence says end/last (or
    start/first) and the target is just that edge scene, say ``last``/``first`` instead. Nothing else is
    rewritten, and a target the operator did write is left alone."""
    out = []
    for raw in raw_ops:
        if isinstance(raw, dict) and raw.get("op") == "move" and _is_int(raw.get("target")):
            t, pos = raw["target"], raw.get("position")
            if pos == "after" and t == count and t not in refs["target"] and _END_CUE.search(prompt):
                raw = {k: v for k, v in raw.items() if k != "target"} | {"position": "last"}
            elif pos == "before" and t == 1 and t not in refs["target"] and _START_CUE.search(prompt):
                raw = {k: v for k, v in raw.items() if k != "target"} | {"position": "first"}
        out.append(raw)
    return out


def _scene_op_errors(op: str, raw: dict, by_number: dict[int, dict], refs: dict[str, set[int]]) -> list[tuple[str, str]]:
    """move / delete / duplicate: which scene, the move's position and target, and the literal-number rule."""
    n = raw.get("scene")
    count = len(by_number)
    if not _is_int(n):
        return [("", f"{op} scene must be a number from 1 to {count}")]
    if n not in by_number:
        if n > count:
            return [(SCENE, f"scene {n} does not exist (the show has {count} scenes)")]
        # Scene 0 or below is the model mislabelling the op, not a scene the operator named: a repair may fix it.
        return [("", f"{op} scene must be a number from 1 to {count}")]
    errors: list[tuple[str, str]] = []
    stray =[k for k in _PATCH_FIELDS if k in raw]
    if op != "move":
        stray += [k for k in ("position", "target") if k in raw]
    if stray:
        errors.append(("", f"{op} takes no {', '.join(stray)}"))
    unwritten = [] if n in refs["delete" if op == "delete" else "move"] else [n]
    example = "delete scene 2"
    if op == "move":
        example = "move scene 2 to the end"
        pos = raw.get("position")
        if pos not in _POSITIONS:
            errors.append(("", "move position must be one of " + ", ".join(_POSITIONS)))
        elif pos in ("before", "after"):
            example = f"move scene 2 {pos} scene 3"
            t = raw.get("target")
            if not _is_int(t):
                errors.append(("", f"move {pos} needs a target scene number from 1 to {count}"))
            elif t not in by_number:
                if t > count:
                    errors.append((SCENE, f"scene {t} does not exist (the show has {count} scenes)"))
                else:
                    errors.append(("", f"move {pos} needs a target scene number from 1 to {count}"))
            elif t == n:
                errors.append(("", f"scene {n} cannot be moved {pos} itself (target equals scene)"))
            elif t not in refs["target"]:
                unwritten.append(t)
        elif "target" in raw:
            errors.append(("", f"move {pos} takes no target"))
    if errors:
        return errors
    if op in ("move", "delete") and unwritten:
        what = "deleting a scene" if op == "delete" else "moving a scene"
        errors.append((NUMBER, f"{what} needs a scene number, e.g. '{example}'"))
    return errors


def _op_errors(raw, scenes_by_number: dict[int, dict], refs: dict[str, set[int]]) -> list[tuple[str, str]]:
    """(kind, message) per problem; kind is SCENE, NOOP, NUMBER or "" for an ordinary schema error."""
    errors: list[tuple[str, str]] = []
    if not isinstance(raw, dict):
        return [("", "each op must be an object")]
    op = raw.get("op")
    if op not in OPS:
        errors.append(("", "op must be one of " + ", ".join(f'"{o}"' for o in OPS)))
        return errors
    for key in _NUMERIC:
        if key in raw and not _is_number(raw[key]):
            errors.append(("", f"{key} must be a number"))
    if "name" in raw and (not isinstance(raw["name"], str) or not raw["name"].strip()):
        errors.append(("", "name must be a non-empty string"))
    if "shape" in raw and raw["shape"] not in SHAPE_TOKENS:
        errors.append(("", f"shape must be one of {sorted(SHAPE_TOKENS)}"))

    if op in _SCENE_OPS:
        return errors + _scene_op_errors(op, raw, scenes_by_number, refs)

    if op == "patch":
        n = raw.get("scene")
        if not isinstance(n, int) or isinstance(n, bool):
            errors.append(("", f"patch scene must be a number from 1 to {len(scenes_by_number)}"))
            return errors
        if n not in scenes_by_number:
            if n > len(scenes_by_number):
                errors.append((SCENE, f"scene {n} does not exist (the show has {len(scenes_by_number)} scenes)"))
            else:
                # Scene 0 or below is the model mislabelling an add, not a scene the operator named.
                errors.append(("", f"patch scene must be a number from 1 to {len(scenes_by_number)}"))
            return errors
        given = [k for k in _PATCH_FIELDS if k in raw]
        if not given:
            errors.append((NOOP, f"patch of scene {n} changes no field"))
        elif not errors:
            cur = scenes_by_number[n]
            if all(raw[k] == cur.get(k) for k in given):
                errors.append((NOOP, f"patch of scene {n} changes nothing (those values are already set)"))
    else:
        n = raw.get("scene")
        if n not in (0, None):
            if isinstance(n, int) and not isinstance(n, bool) and n > len(scenes_by_number):
                # The model reaches for "add" when the operator named a scene that is not there.
                errors.append((SCENE, f"scene {n} does not exist (the show has {len(scenes_by_number)} scenes)"))
            else:
                errors.append(("", "add must use scene 0"))
        if "shape" not in raw:
            errors.append(("", "add needs a shape"))
    return errors


def plan(user_prompt: str, scenes: list[dict], repair_note: str | None = None) -> PlanResult:
    """One planning call. Never raises and never retries; see the module docstring."""
    if asks_for_show_length(user_prompt):
        return PlanResult(status="ok", ops=[], unsupported=[SHOW_LENGTH_UNSUPPORTED])
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
    refs = prompt_refs(user_prompt)
    raw_ops = _normalise_moves(raw_ops, user_prompt, len(scenes), refs)
    errors: list[tuple[str, str]] = []
    for i, raw in enumerate(raw_ops, start=1):
        errors += [(kind, f"op {i}: {msg}") for kind, msg in _op_errors(raw, by_number, refs)]
    names = [r.get("op") for r in raw_ops if isinstance(r, dict)]
    if "move" in names and len(raw_ops) > 1:
        errors.append(("", "a move must be the only op in a draft; return the move alone"))
    if names.count("delete") > 1:
        errors.append(("", "at most 1 delete is allowed per draft"))
    if errors:
        kinds = {kind for kind, _ in errors}
        if kinds <= {SCENE, NUMBER}:
            # A scene the show does not have, or a delete/move whose number the operator never wrote:
            # a repair could only make the model guess, so this is a final answer.
            final = sorted({m.split(": ", 1)[1] for _, m in errors})
            return PlanResult(status="ok", ops=[], unsupported=final)
        if kinds == {NOOP} and len(errors) == len(raw_ops):
            # Every op changes nothing: the request is not an edit of scene shape, size, timing or name.
            return PlanResult(
                status="ok", ops=[],
                unsupported=["no scene would change by this request"],
            )
        return PlanResult(status="schema_error", error="; ".join(msg for _, msg in errors))

    ops = []
    fallback_used = False  # the operator's one stated name goes to at most one scene
    for raw in raw_ops:
        out: dict = {"op": raw["op"], "scene": 0 if raw["op"] == "add" else raw["scene"]}
        if raw["op"] == "move":
            out["position"] = raw["position"]
            if raw["position"] in ("before", "after"):
                out["target"] = raw["target"]
        if "name" in raw:
            out["name"] = raw["name"].strip()
        elif raw["op"] == "add" and not fallback_used:
            fallback_used = True
            named = name_from_prompt(user_prompt)
            if named:
                out["name"] = named
        if "shape" in raw:
            out["shape_type"], out["shape_payload"] = SHAPE_TOKENS[raw["shape"]]
        for key in _NUMERIC:
            if key in raw:
                out[key] = float(raw[key])  # unclamped on purpose
        ops.append(out)
    return PlanResult(status="ok", ops=ops, unsupported=[str(x) for x in unsupported])
