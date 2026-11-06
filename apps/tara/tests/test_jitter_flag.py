"""`tara up --jitter-ms` input checks and messages (issue #207).

`build_services` and the supervisor are patched, so no process is ever started.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from tara_cli import main as cli_main
from tara_cli.main import app

runner = CliRunner()


def _said(result) -> str:
    """The command's output as one line: Typer wraps errors in an 80-column box."""
    return " ".join(result.output.replace("│", " ").split())


@pytest.fixture
def spawned(monkeypatch, tmp_path):
    """Make `up` start nothing; return the keyword arguments of each `build_services` call."""
    calls: list[dict] = []

    def fake_build_services(config, **kwargs):
        calls.append(kwargs)
        return []

    class _Supervisor:
        def __init__(self, *a, **k): ...

        def __getattr__(self, name):
            return lambda *a, **k: 0

    monkeypatch.setattr(cli_main, "build_services", fake_build_services)
    monkeypatch.setattr(cli_main, "Supervisor", _Supervisor, raising=False)
    monkeypatch.setenv("PX4_AUTOPILOT_DIR", str(tmp_path))  # `--backend px4` needs the checkout to exist
    return calls


@pytest.mark.parametrize("value", ["-0.1", "-5", "nan", "inf", "-inf"])
def test_jitter_that_is_negative_or_not_finite_is_rejected_before_anything_starts(spawned, value):
    result = runner.invoke(app, ["up", "--backend", "sim", "-n", "3", "--jitter-ms", value])
    assert result.exit_code == 2
    assert "--jitter-ms: must be a finite number of milliseconds, 0 or more" in _said(result)
    assert spawned == []


@pytest.mark.parametrize("value", ["0", "0.0", "0.5", "3", "12.5"])
def test_finite_jitter_of_zero_or_more_is_accepted_and_passed_on(spawned, value):
    result = runner.invoke(app, ["up", "--backend", "sim", "-n", "3", "--jitter-ms", value])
    assert result.exit_code == 0, result.output
    assert len(spawned) == 1
    assert spawned[0]["sim_jitter_ms"] == float(value)


def test_jitter_defaults_to_zero(spawned):
    result = runner.invoke(app, ["up", "--backend", "sim", "-n", "3"])
    assert result.exit_code == 0, result.output
    assert spawned[0]["sim_jitter_ms"] == 0.0
    assert "--jitter-ms" not in result.output


def test_px4_says_jitter_is_ignored(spawned):
    result = runner.invoke(app, ["up", "--backend", "px4", "-n", "3", "--jitter-ms", "1.5"])
    assert result.exit_code == 0, result.output
    assert "--jitter-ms 1.5 is ignored on --backend px4" in _said(result)


def test_px4_without_jitter_says_nothing_about_it(spawned):
    result = runner.invoke(app, ["up", "--backend", "px4", "-n", "3"])
    assert "--jitter-ms" not in result.output


def test_a_range_error_on_px4_is_still_an_error(spawned):
    """The check does not depend on the backend."""
    result = runner.invoke(app, ["up", "--backend", "px4", "-n", "3", "--jitter-ms", "-1"])
    assert result.exit_code == 2
    assert "--jitter-ms: must be a finite number of milliseconds, 0 or more" in _said(result)
    assert spawned == []


def test_sim_says_the_gcs_does_not_apply_jitter_yet(spawned):
    """The GCS builds the sim fleet's RF channel and does not read SIM_TDMA_JITTER_MS yet; say so."""
    result = runner.invoke(app, ["up", "--backend", "sim", "-n", "3", "--jitter-ms", "1"])
    assert result.exit_code == 0, result.output
    assert "does not apply it yet" in _said(result)
    assert "ignored on --backend" not in result.output
