"""`tara up --packet-loss` input checks (issue #62 retro-review).

`--packet-loss 5.0` used to mean 100 percent loss without a word, and on
`--backend px4` both packet-loss flags were silently ignored. The command now
rejects a nonsense rate before it spawns anything, and says when a flag is
ignored. `build_services` is patched so no process is ever started.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from tara_cli import main as cli_main
from tara_cli.main import app

runner = CliRunner()


@pytest.fixture
def spawned(monkeypatch, tmp_path):
    """Replace service construction and the supervisor so `up` starts nothing.

    Returns the list of ``build_services`` keyword calls made.
    """
    calls: list[dict] = []

    def fake_build_services(config, **kwargs):
        calls.append(kwargs)
        return []

    class _Supervisor:
        def __init__(self, *a, **k): ...
        def add(self, *a, **k): ...
        def run(self, *a, **k):
            return 0

        def __getattr__(self, name):
            return lambda *a, **k: 0

    monkeypatch.setattr(cli_main, "build_services", fake_build_services)
    monkeypatch.setattr(cli_main, "Supervisor", _Supervisor, raising=False)
    # `--backend px4` needs the checkout to exist; the check is not under test.
    monkeypatch.setenv("PX4_AUTOPILOT_DIR", str(tmp_path))
    return calls


@pytest.mark.parametrize("value", ["5.0", "1.0000001", "-0.1", "nan", "inf", "-inf"])
def test_packet_loss_outside_0_to_1_is_rejected_before_anything_starts(spawned, value):
    result = runner.invoke(app, ["up", "--backend", "sim", "-n", "3", "--packet-loss", value])
    assert result.exit_code != 0
    assert "--packet-loss" in result.output
    assert spawned == []


@pytest.mark.parametrize("value", ["0", "0.0", "0.05", "1", "1.0"])
def test_packet_loss_inside_0_to_1_is_accepted(spawned, value):
    result = runner.invoke(app, ["up", "--backend", "sim", "-n", "3", "--packet-loss", value])
    assert "--packet-loss" not in result.output
    assert len(spawned) == 1
    assert spawned[0]["sim_packet_loss"] == float(value)


@pytest.mark.parametrize("value", ["-1", "nan", "inf"])
def test_packet_loss_per_100m_must_be_finite_and_not_negative(spawned, value):
    result = runner.invoke(app, ["up", "--backend", "sim", "-n", "3", "--packet-loss-per-100m", value])
    assert result.exit_code != 0
    assert "--packet-loss-per-100m" in result.output
    assert spawned == []


def test_packet_loss_per_100m_above_one_is_allowed(spawned):
    """A rate per 100 m above 1 is a steep gradient, not a mistake (100 percent after 50 m)."""
    result = runner.invoke(app, ["up", "--backend", "sim", "-n", "3", "--packet-loss-per-100m", "2.5"])
    assert "--packet-loss-per-100m" not in result.output
    assert spawned[0]["sim_packet_loss_per_100m"] == 2.5


@pytest.mark.parametrize("flag", ["--packet-loss", "--packet-loss-per-100m"])
def test_px4_warns_that_a_packet_loss_flag_is_ignored(spawned, flag):
    result = runner.invoke(app, ["up", "--backend", "px4", "-n", "3", flag, "0.1"])
    assert "ignored" in result.output
    assert flag in result.output
    assert "px4" in result.output


def test_px4_without_packet_loss_flags_prints_no_warning(spawned):
    result = runner.invoke(app, ["up", "--backend", "px4", "-n", "3"])
    assert "ignored" not in result.output


def test_sim_with_packet_loss_prints_no_ignored_warning(spawned):
    result = runner.invoke(app, ["up", "--backend", "sim", "-n", "3", "--packet-loss", "0.1"])
    assert "ignored" not in result.output


def test_a_range_error_on_px4_is_still_an_error(spawned):
    """Validation does not depend on the backend."""
    result = runner.invoke(app, ["up", "--backend", "px4", "-n", "3", "--packet-loss", "5"])
    assert result.exit_code != 0
    assert spawned == []
