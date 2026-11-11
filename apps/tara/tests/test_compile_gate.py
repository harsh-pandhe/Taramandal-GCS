"""`tara compile` refuses what Studio would refuse to export (issue #56 audit, finding 2).

Before this, the CLI ran only the altitude and separation checks, so a show with an 80 m/s leg
compiled and wrote `.tarm` files with exit 0 while Studio's `/api/export` returns 412 for it.
The CLI now runs Studio's `certify_feasibility_report` on the show as designed and again on the
compiled bytes decoded back (centimetre rounding can break a marginal show: #157, #175), and it
refuses input that cannot be compiled faithfully (NaN, infinity, null, text, missing keys, LED
values outside 0-255, no drones) with a message that says where.

Every refusal must write nothing: no `.tarm` and no `manifest.json`.
"""

import json
import math
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tara_cli.main import app
from tara_core import MANIFEST_FILENAME, compile_drone_mission, decompile_drone_mission
from tara_studio.validator import certify_feasibility_report

runner = CliRunner()


def _wp(t, x, y=0.0, z=-2.0, **extra):
    return {"time": t, "x": x, "y": y, "z": z, "yaw": 0.0, **extra}


def _show(*waypoints, drone_id=0):
    return {"version": 1, "drones": [{"id": drone_id, "waypoints": list(waypoints)}]}


def _run(tmp_path: Path, show: dict, *flags: str):
    show_file = tmp_path / "show.json"
    # json.dumps writes NaN / Infinity as bare literals and json.loads reads them back, like a real file.
    show_file.write_text(json.dumps(show))
    out_dir = tmp_path / "dist"
    result = runner.invoke(app, ["compile", str(show_file), "--output-dir", str(out_dir), *flags])
    return result, out_dir


def _nothing_written(out_dir: Path) -> bool:
    return not out_dir.exists() or not any(out_dir.iterdir())


# --- Studio's feasibility gate -------------------------------------------------


@pytest.mark.parametrize(
    "name, show, check",
    [
        ("80 m/s dash", _show(_wp(0, 0), _wp(1, 80), _wp(2, 80)), "velocity=80.0, limit=6.0"),
        ("300 m teleport", _show(_wp(0, 0), _wp(0.1, 300)), "velocity=3000.0, limit=6.0"),
    ],
)
def test_a_show_studio_refuses_is_refused_with_the_reason(tmp_path, name, show, check):
    # Control: this is exactly what Studio's export gate says about it.
    certified, _ = certify_feasibility_report(show)
    assert not certified, f"{name} should fail Studio's gate for this test to mean anything"

    result, out_dir = _run(tmp_path, show)
    assert result.exit_code != 0, result.output
    assert "refused" in result.output
    assert check in result.output
    assert "nothing was written" in result.output
    assert _nothing_written(out_dir)


def _rounding_sensitive_show():
    """A smooth path whose max jerk is 16.2 m/s^3 as designed, under the 20 m/s^3 limit, and
    22.5 m/s^3 once every position is rounded to a centimetre in the .tarm. Found by search;
    the control assertions in the test below fail loudly if it ever stops being that."""
    waypoints = [_wp(round(i * 0.2, 4), round(2.0 * math.sin(1.6 * i * 0.2), 4)) for i in range(61)]
    return _show(*waypoints)


def test_a_show_that_only_fails_after_centimetre_rounding_is_refused(tmp_path):
    show = _rounding_sensitive_show()

    # Control 1: it certifies as designed, so the as-designed gate alone would let it through.
    designed_ok, designed = certify_feasibility_report(show)
    assert designed_ok
    assert designed["metrics"]["max_jerk_m_s3"] < 20.0

    # Control 2: the bytes the CLI would write do not.
    mission = compile_drone_mission(show["drones"][0]["waypoints"], drone_id=0, show_id=bytes(16))
    _, decoded = decompile_drone_mission(mission.payload)
    compiled_ok, compiled = certify_feasibility_report({0: decoded})
    assert not compiled_ok
    assert compiled["metrics"]["max_jerk_m_s3"] > 20.0

    result, out_dir = _run(tmp_path, show)
    assert result.exit_code != 0, result.output
    assert "as compiled (after centimetre rounding)" in result.output
    assert "jerk" in result.output
    assert _nothing_written(out_dir)


def test_no_validate_skips_the_gates_only(tmp_path):
    """The existing override still compiles a flyable-on-paper-only show, and says nothing is refused."""
    result, out_dir = _run(tmp_path, _show(_wp(0, 0), _wp(1, 80), _wp(2, 80)), "--no-validate")
    assert result.exit_code == 0, result.output
    assert (out_dir / MANIFEST_FILENAME).is_file()


def test_a_gate_that_cannot_run_refuses_rather_than_passes(tmp_path, monkeypatch):
    import tara_studio.validator as validator

    def broken(*args, **kwargs):
        raise RuntimeError("validator exploded")

    monkeypatch.setattr(validator, "certify_feasibility_report", broken)
    result, out_dir = _run(tmp_path, _show(_wp(0, 0), _wp(2, 1)))
    assert result.exit_code != 0
    assert "could not run the feasibility gate" in result.output
    assert "validator exploded" in result.output
    assert _nothing_written(out_dir)


def test_a_flyable_show_still_compiles_with_the_gate_on(tmp_path):
    """The same smooth path as the rounding test at half the amplitude: comfortably certifiable."""
    waypoints = [_wp(round(i * 0.2, 4), round(1.0 * math.sin(1.6 * i * 0.2), 4)) for i in range(61)]
    show = _show(*waypoints)
    assert certify_feasibility_report(show)[0]
    result, out_dir = _run(tmp_path, show)
    assert result.exit_code == 0, result.output
    assert (out_dir / MANIFEST_FILENAME).is_file()


# --- input that cannot be compiled faithfully ---------------------------------


NAN, INF = float("nan"), float("inf")


@pytest.mark.parametrize(
    "name, show, expected",
    [
        ("NaN x", _show(_wp(0, 0), _wp(1, NAN)), "drone 0 waypoint 1: x is nan, not a finite number"),
        ("NaN time", _show(_wp(0, 0), _wp(NAN, 1)), "drone 0 waypoint 1: time is nan, not a finite number"),
        ("infinite z", _show(_wp(0, 0), _wp(1, 0, z=INF)), "drone 0 waypoint 1: z is inf, not a finite number"),
        ("negative time", _show(_wp(-1, 0), _wp(1, 0)), "drone 0 waypoint 0: time is -1, before the show starts"),
        ("null x", _show(_wp(0, 0), _wp(1, None)), "drone 0 waypoint 1: x is None, not a number"),
        ("text x", _show(_wp(0, 0), _wp(1, "1.0")), "drone 0 waypoint 1: x is '1.0', not a number"),
        ("LED above 255", _show(_wp(0, 0, r=300), _wp(2, 0)), "LED channel r is 300, not a whole number from 0 to 255"),
        ("LED negative", _show(_wp(0, 0, g=-1), _wp(2, 0)), "LED channel g is -1"),
        ("LED fractional", _show(_wp(0, 0, b=1.5), _wp(2, 0)), "LED channel b is 1.5"),
        ("no waypoints", _show(), "drone 0 has no waypoints"),
        ("no drones", {"version": 1, "drones": []}, "the show has no drones"),
    ],
)
@pytest.mark.parametrize("flags", [(), ("--no-validate",)], ids=["gates-on", "no-validate"])
def test_input_that_cannot_be_compiled_faithfully_is_refused_with_where(tmp_path, name, show, expected, flags):
    result, out_dir = _run(tmp_path, show, *flags)
    assert result.exit_code != 0, f"{name}: {result.output}"
    assert f"invalid show: {expected}" in result.output or expected in result.output, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), f"{name}: leaked {result.exception!r}"
    assert _nothing_written(out_dir)


@pytest.mark.parametrize("missing", ["time", "x", "y", "z"])
def test_a_missing_key_is_refused_and_named(tmp_path, missing):
    waypoint = _wp(1, 1)
    del waypoint[missing]
    result, out_dir = _run(tmp_path, _show(_wp(0, 0), waypoint))
    assert result.exit_code != 0
    assert f"missing ['{missing}']" in result.output
    assert _nothing_written(out_dir)


def test_every_problem_is_listed_not_just_the_first(tmp_path):
    show = _show(_wp(0, 0, r=300), _wp(1, NAN), _wp(2, 0, z=INF))
    result, _ = _run(tmp_path, show)
    assert result.exit_code != 0
    assert result.output.count("invalid show:") == 3
