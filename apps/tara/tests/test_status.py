"""Tests for `tara status` / `tara status --json`.

The API probe is exercised against a real `http.server` on a real socket
rather than a mocked `urllib` — the thing worth proving is that the CLI
tolerates the failure modes a health check actually meets (nothing listening,
a 500, a hung server, a garbage body), and a mock of the very layer that
raises those errors would just be asserting the mock.

Bound to port 0 and read back, never a hardcoded port: a fixed one can collide
with a stale listener from a previous failed run and hang confusingly.

The process half of the status is supplied by `check_stray_processes`, which
shells out to `pgrep`. That is monkeypatched here — the tests need to pin
"processes are/aren't running" to reach the exit-code rule, and spawning real
decoy processes matching the kill patterns would be both slower and a genuine
hazard if a test ever left one behind for `tara reset` to find.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tara_cli import status as status_mod
from tara_cli.config import TaraConfig
from tara_cli.main import app
from tara_cli.status import collect_status, to_json, to_text

#: What a healthy gcs-api answers with. Matches the shape of
#: `tara_gcs.main.get_status`, whose own contract is covered by
#: `packages/tara-gcs/tests/test_status_endpoint.py`.
HEALTHY_PAYLOAD = {
    "backend": "sim",
    "num_drones": 5,
    "connected": 5,
    "armed": 3,
    "trajectory_running": False,
}

#: Stand-in for `pgrep -af` output when the stack is up.
RUNNING_PROCESSES = [
    "1234 /repo/.venv/bin/python -m uvicorn tara_gcs.main:app --port 8090",
    "1235 node .../vite --port 5173",
]


def _serve(payload, status_code: int, hang: threading.Event | None):
    """Run a one-route HTTP server on an ephemeral port.

    Returns the server (for teardown) and the OS-assigned port. When ``hang``
    is given the handler blocks on it instead of sleeping a fixed duration, so
    teardown can release the request rather than waiting it out.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if hang is not None:
                hang.wait(timeout=10.0)
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # keep pytest output clean

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


@pytest.fixture
def api_server():
    """Yields a factory for a live stub gcs-api, torn down after the test.

    ``hang=True`` makes the route block until teardown releases it, simulating
    a wedged backend without pinning the test to a fixed sleep.
    """
    servers = []
    released = threading.Event()

    def _start(payload=HEALTHY_PAYLOAD, status_code: int = 200, hang: bool = False) -> int:
        server, port = _serve(payload, status_code, released if hang else None)
        servers.append(server)
        return port

    yield _start

    released.set()  # let any blocked handler finish so shutdown() isn't stuck on it
    for server in servers:
        server.shutdown()
        server.server_close()


def _config(port: int) -> TaraConfig:
    """A config pointing only at ``port``. No repo paths are touched by status."""
    return TaraConfig(repo_root=Path("/nonexistent"), gcs_backend_port=port)


#: A port nothing will ever be listening on. 1 is reserved (tcpmux) and is not
#: bound by anything on a developer machine or in CI.
DEAD_PORT = 1

#: Probe timeout for the "nothing is listening" tests. Linux refuses a dead
#: localhost port instantly, but Windows retries the connect for ~2s before
#: giving up, which would otherwise add several seconds to this file for no
#: added coverage — the assertion is only ever "unreachable", which holds
#: either way. Kept well under `PROBE_TIMEOUT_S` so it cannot mask a
#: regression that makes the probe slow.
DEAD_PORT_TIMEOUT_S = 0.3


@pytest.fixture
def processes(monkeypatch):
    """Yields a setter for what `check_stray_processes` reports.

    Patched in `tara_cli.status`, where it is looked up, not in
    `tara_cli.reset` where it is defined. Defaults to "nothing running" so a
    test that does not care cannot accidentally depend on the real process
    table of whatever machine it runs on.
    """

    def _set(lines: list[str]) -> None:
        monkeypatch.setattr(status_mod, "check_stray_processes", lambda: list(lines))

    _set([])
    return _set


class TestCollectStatus:
    def test_healthy_stack_is_up_with_fleet_counts(self, api_server, processes):
        processes(RUNNING_PROCESSES)
        snapshot = collect_status(_config(api_server()))

        assert snapshot.up is True
        assert snapshot.api_reachable is True
        assert snapshot.processes_running == 2
        assert snapshot.backend == "sim"
        assert snapshot.num_drones == 5
        assert snapshot.connected == 5
        assert snapshot.armed == 3
        assert snapshot.error is None

    def test_nothing_running_is_down_but_not_an_error(self, processes):
        """An idle machine: no processes, no API. Reported down, no fleet data."""
        processes([])
        snapshot = collect_status(_config(DEAD_PORT), timeout=DEAD_PORT_TIMEOUT_S)

        assert snapshot.up is False
        assert snapshot.processes_running == 0
        assert snapshot.api_reachable is False
        assert snapshot.num_drones is None

    def test_processes_alive_but_api_dead_is_the_half_dead_case(self, processes):
        """The failure this command exists to catch — up in name only."""
        processes(RUNNING_PROCESSES)
        snapshot = collect_status(_config(DEAD_PORT), timeout=DEAD_PORT_TIMEOUT_S)

        assert snapshot.up is False
        assert snapshot.processes_running == 2
        assert snapshot.api_reachable is False
        assert snapshot.error and "cannot reach" in snapshot.error

    def test_http_error_from_the_api_counts_as_unreachable(self, api_server, processes):
        """A backend that is listening but erroring is not healthy."""
        processes(RUNNING_PROCESSES)
        snapshot = collect_status(_config(api_server(status_code=500)))

        assert snapshot.up is False
        assert snapshot.api_reachable is False
        assert "HTTP 500" in snapshot.error

    def test_malformed_body_does_not_crash_the_probe(self, api_server, processes):
        """A truncated or non-JSON response must report, not raise."""
        processes(RUNNING_PROCESSES)
        snapshot = collect_status(_config(api_server(payload=b"not json at all")))

        assert snapshot.up is False
        assert snapshot.api_reachable is False
        assert "malformed response" in snapshot.error

    def test_hung_api_fails_fast_instead_of_blocking(self, api_server, processes):
        """A wedged backend must time out — this runs in scripts and CI loops."""
        processes(RUNNING_PROCESSES)
        started = time.monotonic()
        snapshot = collect_status(_config(api_server(hang=True)), timeout=0.3)
        elapsed = time.monotonic() - started

        assert snapshot.api_reachable is False
        assert snapshot.up is False
        assert elapsed < 3.0, "probe waited on the hung server instead of timing out"


class TestRendering:
    def test_json_is_parseable_and_carries_every_field(self, api_server, processes):
        processes(RUNNING_PROCESSES)
        parsed = json.loads(to_json(collect_status(_config(api_server()))))

        assert parsed["up"] is True
        assert parsed["backend"] == "sim"
        assert parsed["num_drones"] == 5
        assert parsed["connected"] == 5
        assert parsed["armed"] == 3
        assert parsed["processes_running"] == 2

    def test_json_when_down_keeps_the_keys_with_nulls(self, processes):
        """Scripts index these keys unconditionally, so they must always exist."""
        processes([])
        parsed = json.loads(to_json(collect_status(_config(DEAD_PORT), timeout=DEAD_PORT_TIMEOUT_S)))

        assert parsed["up"] is False
        for key in ("backend", "num_drones", "connected", "armed"):
            assert key in parsed and parsed[key] is None

    def test_text_report_names_the_counts(self, api_server, processes):
        processes(RUNNING_PROCESSES)
        text = to_text(collect_status(_config(api_server())))

        assert "backend=sim" in text
        assert "drones=5" in text
        assert "connected=5" in text
        assert "armed=3" in text

    def test_text_report_when_idle_says_nothing_running(self, processes):
        processes([])
        assert to_text(collect_status(_config(DEAD_PORT), timeout=DEAD_PORT_TIMEOUT_S)) == "[tara] nothing running."

    def test_text_report_flags_an_unreachable_api(self, processes):
        processes(RUNNING_PROCESSES)
        text = to_text(collect_status(_config(DEAD_PORT), timeout=DEAD_PORT_TIMEOUT_S))

        assert "UNREACHABLE" in text
        assert "2 process(es) running" in text


class TestCommand:
    """The CLI surface: both output modes and the exit code contract."""

    def _invoke(self, monkeypatch, port: int, args: list[str]):
        monkeypatch.setattr("tara_cli.main.load_config", lambda: _config(port))
        return CliRunner().invoke(app, ["status", *args])

    def test_human_output_is_the_default(self, api_server, processes, monkeypatch):
        processes(RUNNING_PROCESSES)
        result = self._invoke(monkeypatch, api_server(), [])

        assert result.exit_code == 0
        assert "backend=sim" in result.stdout
        assert not result.stdout.lstrip().startswith("{")

    def test_json_flag_emits_json(self, api_server, processes, monkeypatch):
        processes(RUNNING_PROCESSES)
        result = self._invoke(monkeypatch, api_server(), ["--json"])

        assert result.exit_code == 0
        assert json.loads(result.stdout)["connected"] == 5

    def test_exits_nonzero_when_expected_up_but_unreachable(self, processes, monkeypatch):
        """The acceptance criterion: running processes + dead API is a failure."""
        processes(RUNNING_PROCESSES)
        result = self._invoke(monkeypatch, DEAD_PORT, ["--json"])

        assert result.exit_code == 1
        assert json.loads(result.stdout)["up"] is False

    def test_exits_zero_when_the_stack_is_simply_not_running(self, processes, monkeypatch):
        """Down-by-choice is not a failure — otherwise every idle CI job fails."""
        processes([])
        result = self._invoke(monkeypatch, DEAD_PORT, [])

        assert result.exit_code == 0
