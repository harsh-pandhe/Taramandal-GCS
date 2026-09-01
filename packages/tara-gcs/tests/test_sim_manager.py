"""Tests for SimManager — the process layer behind the dashboard's sim controls.

The acceptance criteria for issue #2 are process-level ("registers active
PIDs", "kills all child processes without leaving zombie instances"), so the
lifecycle tests here spawn real OS processes and assert on the real process
table. An in-process test with a mocked subprocess would prove that the code
calls `killpg`, which is not the thing that has ever been in doubt — what
matters is whether a *tree* of grandchildren actually dies, and that only
exists across a real fork.

PX4 and Gazebo are not available in CI (or on a dev laptop without a PX4
build), so the lifecycle tests drive the same manager against a stand-in
script that forks children and ignores SIGINT/SIGTERM the way a wedged
simulator does. That is deliberately the harder case: it forces the full
SIGINT -> SIGTERM -> SIGKILL escalation the real thing only occasionally
needs.

Validation and status tests need no processes at all and run everywhere.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tara_gcs.sim_manager import (
    PX4_MAX_DRONES,
    SimLaunchError,
    SimManager,
    validate_drone_count,
)

#: Process-spawning tests only. killpg/SIGKILL/start_new_session have no
#: Windows equivalent, and neither does the simulation stack under test.
posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="SimManager's process control is POSIX-only (killpg/SIGKILL/setsid)",
)


def _pid_alive(pid: int) -> bool:
    """True if ``pid`` is a live, non-zombie process.

    The zombie check is the point of this helper rather than a bare
    `os.kill(pid, 0)`: a reaped-but-not-waited child still answers signal 0,
    so "no zombies left behind" would pass trivially without inspecting state.
    """
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    # /proc is Linux-only; on macOS fall back to ps.
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            return stat.read_text().rsplit(") ", 1)[1].split()[0] != "Z"
        except (IndexError, OSError):
            return False
    out = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    state = out.stdout.strip()
    return bool(state) and not state.startswith("Z")


def _descendants(pid: int) -> list[int]:
    """Every live descendant of ``pid``, transitively — same approach as
    apps/tara/tests/test_up_down_lifecycle.py."""
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

    found, stack = [], [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            found.append(child)
            stack.append(child)
    return found


@pytest.fixture
def stubbed_repo(tmp_path: Path) -> Path:
    """A repo-shaped tmp dir whose sim scripts stand in for PX4/Gazebo.

    `launch_fleet.sh` here forks two background children and then traps and
    *ignores* SIGINT and SIGTERM, imitating a simulator wedged badly enough to
    need SIGKILL. The children are plain `sleep`s that inherit the process
    group, which is what makes "did the whole group die" a real assertion
    rather than a check on the parent alone.
    """
    px4 = tmp_path / "sim" / "px4"
    px4.mkdir(parents=True)

    (px4 / "launch_fleet.sh").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            # Stand-in for PX4/Gazebo: forks children, then refuses to die politely.
            trap '' SIGINT SIGTERM
            sleep 300 &
            sleep 300 &
            echo "stub fleet up with $# args"
            wait
            """
        )
    )
    (px4 / "stop_fleet.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    for script in px4.glob("*.sh"):
        script.chmod(0o755)

    ros = tmp_path / "sim" / "ros"
    ros.mkdir(parents=True)
    (ros / "launch_rviz.sh").write_text("#!/usr/bin/env bash\nsleep 300\n")
    (ros / "launch_rviz.sh").chmod(0o755)

    return tmp_path


# --- validation (no processes, runs everywhere) ---------------------------


def test_px4_rejects_fleets_above_the_sitl_ceiling():
    """500 drones on real SITL is a request to wedge the machine, not a launch."""
    with pytest.raises(SimLaunchError, match="exceeds the real-SITL ceiling"):
        validate_drone_count(500, "px4")


def test_sim_backend_accepts_the_large_presets():
    """The same count the px4 backend refuses is fine on the vectorized fleet."""
    validate_drone_count(500, "sim")  # must not raise


def test_px4_accepts_counts_at_its_ceiling():
    validate_drone_count(PX4_MAX_DRONES, "px4")  # must not raise


@pytest.mark.parametrize("count", [0, -1])
def test_non_positive_counts_are_rejected(count):
    with pytest.raises(SimLaunchError, match="at least 1"):
        validate_drone_count(count, "sim")


def test_unknown_backend_is_rejected():
    with pytest.raises(SimLaunchError, match="unknown backend"):
        validate_drone_count(5, "ardupilot")


@pytest.mark.asyncio
async def test_sim_backend_launch_spawns_nothing_but_reports_running(tmp_path):
    """The vectorized fleet lives in the API process; there is no child to spawn.

    Still has to report RUNNING, because the dashboard's pill is driven by this
    status and an operator who launched a 500-drone sim fleet should not see
    INACTIVE.
    """
    manager = SimManager(repo_root=tmp_path)
    status = await manager.launch(num_drones=500, backend="sim")

    assert status["running"] is True
    assert status["backend"] == "sim"
    assert status["num_drones"] == 500
    assert status["processes"] == []
    assert status["mavlink_ports"] == []  # no network layer on this backend


@pytest.mark.asyncio
async def test_status_on_idle_manager_reports_inactive(tmp_path):
    status = SimManager(repo_root=tmp_path).status()
    assert status["running"] is False
    assert status["num_drones"] == 0
    assert status["processes"] == []


# --- lifecycle (real processes) -------------------------------------------


@posix_only
@pytest.mark.asyncio
async def test_launch_registers_active_pids(stubbed_repo):
    """POST /api/sim/launch's core: after launching, the manager knows the PIDs.

    Asserts the registered pid is a real live process and that the MAVLink port
    block matches the fleet size, which is what the dashboard reports as
    "NODES".
    """
    manager = SimManager(repo_root=stubbed_repo)
    try:
        status = await manager.launch(num_drones=3, backend="px4", headless=True)

        assert status["running"] is True
        assert status["backend"] == "px4"
        assert len(status["processes"]) == 1

        fleet = status["processes"][0]
        assert fleet["name"] == "fleet"
        assert fleet["running"] is True
        assert _pid_alive(fleet["pid"])
        assert status["mavlink_ports"] == [14540, 14541, 14542]
    finally:
        await manager.stop()


@posix_only
@pytest.mark.asyncio
async def test_launch_refuses_a_second_concurrent_fleet(stubbed_repo):
    """Two PX4 fleets collide on MAVLink ports and the Gazebo partition."""
    manager = SimManager(repo_root=stubbed_repo)
    try:
        await manager.launch(num_drones=2, backend="px4")
        with pytest.raises(SimLaunchError, match="already running"):
            await manager.launch(num_drones=2, backend="px4")
    finally:
        await manager.stop()


@posix_only
@pytest.mark.asyncio
async def test_stop_kills_the_whole_process_tree_without_zombies(stubbed_repo):
    """The issue's second acceptance criterion, checked against the process table.

    The stub traps and ignores SIGINT and SIGTERM, so passing this requires the
    escalation to actually reach SIGKILL — and requires the signal to go to the
    process *group*, since the two forked `sleep` children are what would
    survive a kill aimed at the shell's pid alone.
    """
    manager = SimManager(repo_root=stubbed_repo)
    await manager.launch(num_drones=2, backend="px4")

    parent_pid = manager.processes["fleet"].pid
    # Give the stub a moment to fork its children before snapshotting the tree.
    await asyncio.sleep(1.0)
    children = _descendants(parent_pid)
    assert children, "stub fleet should have forked children to kill"

    result = await manager.stop()

    assert [p["pid"] for p in result["stopped"]] == [parent_pid]
    assert result["status"]["running"] is False
    assert result["status"]["processes"] == []
    assert manager.processes == {}

    # Nothing left alive, and nothing left as a zombie either.
    assert not _pid_alive(parent_pid), "fleet parent survived stop()"
    for child in children:
        assert not _pid_alive(child), f"orphaned child {child} survived stop()"


@posix_only
@pytest.mark.asyncio
async def test_stop_is_safe_when_nothing_is_running(stubbed_repo):
    """The dashboard's Terminate button is always clickable; it must not 500."""
    manager = SimManager(repo_root=stubbed_repo)
    result = await manager.stop()
    assert result["stopped"] == []
    assert result["status"]["running"] is False


@posix_only
@pytest.mark.asyncio
async def test_rviz_requires_a_running_fleet(stubbed_repo):
    """RViz visualizes Gazebo TF — without a fleet there is nothing to display."""
    manager = SimManager(repo_root=stubbed_repo)
    with pytest.raises(SimLaunchError, match="no PX4 fleet is running"):
        await manager.launch_rviz()


@posix_only
@pytest.mark.asyncio
async def test_rviz_launches_alongside_a_running_fleet(stubbed_repo):
    manager = SimManager(repo_root=stubbed_repo)
    try:
        await manager.launch(num_drones=2, backend="px4")
        status = await manager.launch_rviz()

        assert status["rviz_running"] is True
        names = {p["name"] for p in status["processes"]}
        assert names == {"fleet", "rviz"}
    finally:
        await manager.stop()


@posix_only
@pytest.mark.asyncio
async def test_status_reaps_a_process_that_exited_on_its_own(tmp_path):
    """A crashed fleet must stop being reported as RUNNING.

    Uses a script that exits immediately rather than the long-lived stub: the
    case under test is the manager noticing an exit it did not cause.
    """
    px4 = tmp_path / "sim" / "px4"
    px4.mkdir(parents=True)
    (px4 / "launch_fleet.sh").write_text("#!/usr/bin/env bash\nexit 1\n")
    (px4 / "stop_fleet.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    for script in px4.glob("*.sh"):
        script.chmod(0o755)

    manager = SimManager(repo_root=tmp_path)
    await manager.launch(num_drones=1, backend="px4")

    # Wait for the doomed script to actually exit before asking for status.
    await asyncio.wait_for(manager.processes["fleet"].process.wait(), timeout=10.0)

    status = manager.status()
    assert status["running"] is False
    assert status["processes"] == []


@posix_only
@pytest.mark.asyncio
async def test_launch_writes_a_retrievable_log(stubbed_repo):
    """Launch output goes to a file so a failed launch is diagnosable from the UI."""
    manager = SimManager(repo_root=stubbed_repo)
    try:
        status = await manager.launch(num_drones=2, backend="px4")
        log_path = Path(status["processes"][0]["log_path"])
        await asyncio.sleep(1.0)  # let the stub write its line

        assert log_path.exists()
        assert "stub fleet up" in log_path.read_text()
    finally:
        await manager.stop()
