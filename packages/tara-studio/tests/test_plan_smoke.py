"""Tests for tools/plan_smoke.py. A fake planner only: nothing here talks to Ollama."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tara_studio.ai_plan import PlanResult

SCRIPT = Path(__file__).resolve().parents[3] / "tools" / "plan_smoke.py"
spec = importlib.util.spec_from_file_location("plan_smoke", SCRIPT)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class FakePlanner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, sentence, scenes):
        self.calls.append(sentence)
        return self.results.pop(0)


def test_run_smoke_returns_one_row_per_sentence():
    fake = FakePlanner([
        PlanResult(status="ok", ops=[{"op": "patch", "scene": 1}], unsupported=[]),
        PlanResult(status="schema_error", error="bad"),
    ])
    rows = smoke.run_smoke(["a", "b"], smoke.DEFAULT_SCENES, planner=fake)
    assert [r["sentence"] for r in rows] == ["a", "b"]
    assert [r["status"] for r in rows] == ["ok", "schema_error"]
    assert rows[0]["ops"] == [{"op": "patch", "scene": 1}] and rows[1]["error"] == "bad"
    assert all(isinstance(r["seconds"], float) for r in rows)


def test_run_smoke_stops_at_the_first_unreachable_answer():
    fake = FakePlanner([PlanResult(status="unavailable", error="refused"), PlanResult(status="ok")])
    rows = smoke.run_smoke(["a", "b", "c"], smoke.DEFAULT_SCENES, planner=fake)
    assert len(rows) == 1 and fake.calls == ["a"]


def test_the_default_list_has_the_required_sentences_and_scenes():
    for s in ("make scene 2 a heart", "add a sphere scene called Finale", 'add a circle scene named "Big Ring"',
              "make the show 2 minutes long", "make scene 9 a circle", "set scale of scene 1 to 5"):
        assert s in smoke.DEFAULT_SENTENCES
    assert [(s["number"], s["name"], s["shape"]) for s in smoke.DEFAULT_SCENES] == [
        (1, "Opening", "circle"), (2, "Middle", "heart"), (3, "Finale", "sphere")]


def test_main_reads_a_sentence_file_skipping_comments_and_blanks(tmp_path, capsys):
    f = tmp_path / "s.txt"
    f.write_text("# header\n\nmake scene 1 a heart\n  add a circle scene  \n", encoding="utf-8")
    fake = FakePlanner([PlanResult(status="ok"), PlanResult(status="ok")])
    assert smoke.main([str(f)], planner=fake) == 0
    assert fake.calls == ["make scene 1 a heart", "add a circle scene"]
    out = capsys.readouterr().out
    assert "make scene 1 a heart" in out and "2 sentences, 2 ok, 0 not ok" in out


@pytest.mark.parametrize("status", ["unavailable", "timeout"])
def test_main_reports_an_unreachable_model_without_a_traceback(status, capsys):
    fake = FakePlanner([PlanResult(status=status, error="connection refused")])
    assert smoke.main([], planner=fake) == 2
    err = capsys.readouterr().err
    assert "not usable" in err and "connection refused" in err and "Traceback" not in err


def test_main_with_the_real_planner_and_no_ollama_exits_cleanly(monkeypatch, capsys):
    # The real plan() against a dead port: no model needed, and no traceback.
    from tara_studio import ai_author, ai_plan
    monkeypatch.setattr(ai_author, "OLLAMA_URL", "http://127.0.0.1:9")
    assert smoke.main([], planner=ai_plan.plan) == 2
    assert "Traceback" not in capsys.readouterr().err


def test_printing_survives_a_console_that_cannot_encode_the_text(monkeypatch):
    import io
    import sys

    cp1252 = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    monkeypatch.setattr(sys, "stdout", cp1252)
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict"))
    fake = FakePlanner([PlanResult(status="ok", error="Finále ✨")] * len(smoke.DEFAULT_SENTENCES))
    assert smoke.main([], planner=fake) == 0  # would raise UnicodeEncodeError without errors="replace"


def test_main_with_a_missing_file_exits_1(tmp_path, capsys):
    assert smoke.main([str(tmp_path / "nope.txt")]) == 1
    assert "cannot read input" in capsys.readouterr().err
