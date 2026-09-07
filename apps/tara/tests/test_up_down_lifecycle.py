"""End-to-end lifecycle test for `tara up` and its shutdown signal.

The process-group start/stop logic in :mod:`tara_cli.process` -- ``setsid`` per
service, SIGTERM to each group, SIGKILL after ``SHUTDOWN_GRACE_S`` -- had only
ever been checked by hand with ``pgrep``. This test does it automatically: it
spawns the real CLI as a subprocess, waits for the stack to come up, sends the
shutdown signal, and asserts nothing is left running.

Why the real CLI and not a ``Supervisor`` built in-process: the thing being
proven is that a signal delivered to *the CLI process from outside* tears down
a tree of grandchildren (vite spawns esbuild helpers; each service is its own
session leader). An in-process test would share the test runner's own process
group and never cross the OS boundary the bug would live at.

POSIX only. ``start_new_session``/``killpg``/``SIGKILL`` have no Windows
equivalent, and neither does the code under test, so this module skips there
rather than pretending to cover it.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tara_cli.config import find_repo_root, load_config
from tara_cli.process import SHUTDOWN_GRACE_S

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="tara_cli.process is POSIX-only (setsid/killpg/SIGKILL)",
    ),
]

#: Fleet size. Small on purpose -- this test is about process lifecycle, not
#: scale, and the sim backend's cost is per-drone.
NUM_DRONES = 3

#: How long the whole stack gets to report healthy. Dominated by vite's
#: cold-start dependency optimization on the two frontends, not by anything
#: Python does.
STARTUP_TIMEOUT_S = 120.0

#: Slack on top of SHUTDOWN_GRACE_S before a leftover process counts as a leak.
#: The supervisor escalates SIGTERM -> wait(grace) -> SIGKILL, so a correct
#: implementation finishes well inside grace + this.
SHUTDOWN_SLACK_S = 15.0


def _port_is_free(port: int) -> bool:
    """True if nothing is currently listening on ``port``.

    The CLI resolves its own repo root from ``tara_cli.config.__file__`` and
    derives both ``venv_python`` and every service path from it, so a run
    cannot be redirected at a throwaway tara.toml on temp ports without adding
    a ``--config`` flag to production code -- out of scope for a test-only
    issue. This test therefore uses the repo's real ports, and checks them up
    front so a developer with a real stack already running gets a clear skip
    instead of a confusing failure over a port collision.
    """
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _descendants(pid: int) -> set[int]:
    """Return every live descendant of ``pid``, transitively.

    Shells out to ``ps`` rather than adding a psutil dependency -- the repo
    does not have one, and this is the only place that needs a process tree.
    """
    result = subprocess.run(
        ["ps", "-eo", "pid=,ppid="], capture_output=True, text=True, check=False
    )
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            child, parent = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(parent, []).append(child)

    seen: set[int] = set()
    stack = [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


def _alive(pid: int) -> bool:
    """True if ``pid`` exists and is not an unreaped zombie.

    A killed child the CLI never wait()ed on lingers as a zombie, and
    ``os.kill(pid, 0)`` still succeeds for one. That would read as a leaked
    process when it is really just an unreaped exit status, so state Z counts
    as gone.
    """
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    state = result.stdout.strip()
    return bool(state) and not state.startswith("Z")


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _http_answers(url: str) -> bool:
    """True if ``url`` answers at all -- any HTTP status counts.

    The question is "did something bind this port and speak HTTP", not "did the
    route succeed", so a 404/405 from a live server is a pass. Only a connection
    error means nothing is listening yet.
    """
    try:
        with urllib.request.urlopen(url, timeout=2):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError):
        return False


@pytest.fixture(scope="module")
def repo_root() -> Path:
    return find_repo_root(Path(__file__))


@pytest.fixture(scope="module")
def frontend_deps(repo_root: Path) -> None:
    """Ensure both vite apps have node_modules, since `tara up` runs them.

    ``npm ci`` is only paid when a tree is actually missing, so the usual case
    on a developer machine or a warm CI cache is a no-op.
    """
    if shutil.which("npm") is None:
        pytest.skip("npm not installed; `tara up` cannot start its frontends")

    for pkg in ("tara-web", "tara-studio-web"):
        pkg_dir = repo_root / "packages" / pkg
        if (pkg_dir / "node_modules").is_dir():
            continue
        result = subprocess.run(
            ["npm", "ci"], cwd=pkg_dir, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            pytest.skip(f"npm ci failed in {pkg}: {result.stderr[-500:]}")


def _wait_for_stack(proc: subprocess.Popen, urls: list[str]) -> None:
    """Block until every URL answers, failing fast if the CLI dies first."""
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    pending = list(urls)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(
                f"`tara up` exited with code {proc.returncode} before the stack "
                f"came up; still waiting on {pending}"
            )
        pending = [u for u in pending if not _http_answers(u)]
        if not pending:
            return
        time.sleep(1.0)
    pytest.fail(
        f"stack did not become healthy within {STARTUP_TIMEOUT_S}s; still down: {pending}"
    )


def _force_cleanup(proc: subprocess.Popen) -> None:
    """Best-effort teardown so a failing assertion cannot leak the stack itself."""
    for pid in _descendants(proc.pid):
        _kill(pid)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


@pytest.mark.parametrize(
    "stop_signal",
    [
        pytest.param(signal.SIGINT, id="ctrl-c"),
        pytest.param(signal.SIGTERM, id="tara-down"),
    ],
)
def test_up_then_down_leaves_no_stray_processes(
    repo_root: Path, frontend_deps: None, stop_signal: signal.Signals
) -> None:
    """`tara up` comes up healthy, then the shutdown signal reaps the whole tree.

    Both signals are exercised because they are the two real ways the stack
    stops -- Ctrl+C in a terminal, and a `kill` from a supervisor or a wrapper
    `tara down`. :meth:`Supervisor.run_until_exit_or_signal` installs the same
    handler for each, so a regression wiring up only one would otherwise pass.
    """
    config = load_config(repo_root)
    ports = {
        "gcs_backend": config.gcs_backend_port,
        "studio_backend": config.studio_backend_port,
        "gcs_web": config.gcs_web_port,
        "studio_web": config.studio_web_port,
    }

    busy = sorted(p for p in ports.values() if not _port_is_free(p))
    if busy:
        pytest.skip(f"ports already in use (is a stack running?): {busy}")

    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            str(repo_root / p)
            for p in (
                "apps/tara",
                "packages/tara-core",
                "packages/tara-gcs",
                "packages/tara-studio",
            )
        ),
        "PYTHONUNBUFFERED": "1",
    }

    proc = subprocess.Popen(
        [
            str(config.venv_python), "-m", "tara_cli", "up",
            "--backend", "sim", "-n", str(NUM_DRONES),
        ],
        cwd=repo_root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    try:
        _wait_for_stack(
            proc,
            [
                f"http://127.0.0.1:{ports['gcs_backend']}/docs",
                f"http://127.0.0.1:{ports['studio_backend']}/docs",
                f"http://127.0.0.1:{ports['gcs_web']}/",
                f"http://127.0.0.1:{ports['studio_web']}/",
            ],
        )
        # Snapshot the tree while it is healthy: this is what must be gone
        # afterwards, and it is the only moment the grandchildren (vite's
        # esbuild helpers) are observable.
        tracked = _descendants(proc.pid)
        assert tracked, "expected `tara up` to have spawned child processes"
    except BaseException:
        _force_cleanup(proc)
        raise

    proc.send_signal(stop_signal)

    grace = SHUTDOWN_GRACE_S + SHUTDOWN_SLACK_S
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        _force_cleanup(proc)
        pytest.fail(f"`tara up` did not exit within {grace}s of {stop_signal.name}")

    # The CLI is gone; its children get the same budget to finish dying, since
    # the supervisor's SIGKILL escalation is what closes out the stubborn ones.
    deadline = time.monotonic() + grace
    leaked = {pid for pid in tracked if _alive(pid)}
    while leaked and time.monotonic() < deadline:
        time.sleep(0.5)
        leaked = {pid for pid in tracked if _alive(pid)}

    if leaked:
        detail = subprocess.run(
            ["ps", "-o", "pid=,stat=,args=", "-p", ",".join(str(p) for p in sorted(leaked))],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        for pid in leaked:
            _kill(pid)
        pytest.fail(f"{len(leaked)} process(es) survived {stop_signal.name}:\n{detail}")
