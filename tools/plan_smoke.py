#!/usr/bin/env python3
"""Run sentences against the REAL local planner model and print what comes back.

    python tools/plan_smoke.py [sentences.txt] [--scenes scenes.json]

Calls ``tara_studio.ai_plan.plan()`` directly (no server). Needs a local Ollama with
qwen2.5:7b (override with TARA_AI_MODEL / TARA_OLLAMA_URL). One sentence per line in
the file; blank lines and lines starting with ``#`` are skipped. With no file the
built-in list below is used. Each sentence makes at most one model call (none for a
show-length request, which the planner answers itself).

Note: this is the planner alone, ONE model call per sentence. In the app a draft that fails the schema
check is sent back to the model once, so a ``schema_error`` here is not necessarily what an operator sees.
The built-in scenes have no scene called "Finale" on purpose: a sentence like "add a sphere scene called
Finale" would otherwise be about a scene that already exists.

This is a manual check, not a test: pytest never runs it against a real model
(tests/test_plan_smoke.py only exercises ``run_smoke`` with a fake planner).
Exit status: 0 ok, 2 Ollama unreachable / timed out, 1 bad input.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

try:
    from tara_studio.ai_plan import plan
except ModuleNotFoundError:  # run from a checkout without the package installed
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "tara-studio"))
    from tara_studio.ai_plan import plan

DEFAULT_SCENES = [
    {"number": 1, "name": "Opening", "shape": "circle", "scale": 1.0, "hold_seconds": 6.0, "transition_seconds": 4.0},
    {"number": 2, "name": "Middle", "shape": "heart", "scale": 1.0, "hold_seconds": 6.0, "transition_seconds": 4.0},
    {"number": 3, "name": "Closing", "shape": "sphere", "scale": 1.0, "hold_seconds": 6.0, "transition_seconds": 4.0},
]

DEFAULT_SENTENCES = [
    "make scene 2 a heart",
    "set the hold of scene 1 to 8 seconds",
    "add a sphere scene called Finale",
    'add a circle scene named "Big Ring"',
    "make scene 1 bigger",
    "make scene 9 a circle",
    "set scale of scene 1 to 5",
    "make the show 2 minutes long",
    "turn the drones red",
    "add a heart scene",
]

UNREACHABLE = ("unavailable", "timeout")


def run_smoke(sentences, scenes, planner=plan):
    """One row per sentence: {sentence, status, ops, unsupported, error, seconds}.

    Stops after the first unreachable/timed-out answer: the rest would fail the same way.
    """
    rows = []
    for sentence in sentences:
        started = time.perf_counter()
        result = planner(sentence, scenes)
        rows.append({
            "sentence": sentence,
            "status": result.status,
            "ops": result.ops,
            "unsupported": result.unsupported,
            "error": result.error,
            "seconds": round(time.perf_counter() - started, 2),
        })
        if result.status in UNREACHABLE:
            break
    return rows


def read_sentences(path: Path) -> list[str]:
    lines = (ln.strip() for ln in path.read_text(encoding="utf-8").splitlines())
    return [ln for ln in lines if ln and not ln.startswith("#")]


def format_row(row: dict) -> str:
    return "\n".join([
        f'> {row["sentence"]}',
        f'  status:      {row["status"]}   ({row["seconds"]} s)',
        f'  ops:         {json.dumps(row["ops"])}',
        f'  unsupported: {json.dumps(row["unsupported"])}',
        f'  error:       {row["error"]}',
    ])


def _make_output_safe() -> None:
    """A Windows console may not encode what the model or a scene name contains."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="replace")


def main(argv=None, planner=plan) -> int:
    _make_output_safe()
    ap = argparse.ArgumentParser(description="Run sentences against the real local planner model.")
    ap.add_argument("sentences", nargs="?", type=Path, help="text file, one sentence per line (# comments allowed)")
    ap.add_argument("--scenes", type=Path, help="JSON file: a list of scenes as the planner receives them")
    args = ap.parse_args(argv)

    try:
        sentences = read_sentences(args.sentences) if args.sentences else DEFAULT_SENTENCES
        scenes = json.loads(args.scenes.read_text(encoding="utf-8")) if args.scenes else DEFAULT_SCENES
    except (OSError, ValueError) as exc:
        print(f"plan_smoke: cannot read input: {exc}", file=sys.stderr)
        return 1
    if not sentences:
        print("plan_smoke: no sentences to run", file=sys.stderr)
        return 1

    rows = run_smoke(sentences, scenes, planner)
    for row in rows:
        print(format_row(row))
        print()
    last = rows[-1]
    if last["status"] in UNREACHABLE:
        print(
            f'plan_smoke: the local model is not usable ({last["status"]}: {last["error"]}). '
            "Start Ollama and pull the model (qwen2.5:7b), then run again.",
            file=sys.stderr,
        )
        return 2
    ok = sum(1 for r in rows if r["status"] == "ok")
    total_s = round(sum(r["seconds"] for r in rows), 1)
    print(f"{len(rows)} sentences, {ok} ok, {len(rows) - ok} not ok, {total_s} s total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
