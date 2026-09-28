"""`tara reset` — kill every stray process this project might have left running.

Covers the same PX4/Gazebo patterns as ``sim/px4/stop_fleet.sh`` (kept as a
standalone script too, since ``launch_fleet.sh`` calls it directly) plus the
uvicorn/vite processes `tara up` starts, in case a previous run was killed hard
enough that :class:`~tara_cli.process.Supervisor` never got to shut them down.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

#: Substrings matched against each running process's command line.
#:
#: "px4" must NOT appear bare here: sim/px4/launch_fleet.sh and stop_fleet.sh
#: both live at a path containing "px4", so `pkill -f px4` (or pgrep) matches
#: their own invocation and kills the launcher instead of just the simulated
#: vehicles. Match the compiled binary path instead — see stop_fleet.sh.
_KILL_PATTERNS = [
    "px4_sitl_default/bin/px4",
    "mavsdk_server",
    "gz sim",
    "gz-sim-server",
    "gz sim-server",
    "ruby.*gz",
    "uvicorn tara_gcs.main:app",
    "uvicorn tara_studio.server:app",
    "vite.*--port 5173",
    "vite.*--port 5174",
    "rviz2",
    "gz_tf_bridge.py",
]


def reset_all() -> None:
    """Force-kill every known stray process and clear Gazebo's lock files."""
    for pattern in _KILL_PATTERNS:
        subprocess.run(["pkill", "-9", "-f", pattern], check=False)

    lock_glob = Path.home() / ".gazebo" / "sys"
    if lock_glob.exists():
        for lock in lock_glob.glob("lock*"):
            lock.unlink(missing_ok=True)

    print("[tara] killed all known stray processes and cleared Gazebo locks.")


def check_stray_processes() -> list[str]:
    """Return command lines of any matching process still running, for `tara status`."""
    found = []
    for pattern in _KILL_PATTERNS:
        result = subprocess.run(
            ["pgrep", "-af", pattern], capture_output=True, text=True, check=False
        )
        found.extend(line for line in result.stdout.splitlines() if line.strip())
    return found
