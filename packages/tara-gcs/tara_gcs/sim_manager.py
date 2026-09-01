"""Launches and tears down the simulation stack from the GCS, not a terminal.

Running a show previously meant `tara up` in a terminal the operator had to
keep open and understand. A show director does not have a terminal; they have
the dashboard. This module is the process layer behind the dashboard's
Simulation Control Bar — start PX4/Gazebo SITL, start RViz, report what's
running, kill it all.

**Why a separate manager and not `tara_cli.process.Supervisor`.** The
supervisor is a foreground, blocking, thread-per-service design: it owns the
terminal, streams interleaved output to stdout, and stops when that terminal's
process gets a signal. Everything here is the opposite — non-blocking spawn
from inside a running event loop, no output pumping (logs go to files the
operator can fetch later), and a lifetime that outlives any single HTTP
request. Sharing code between the two would mean bending one to the other's
shape; they are ~80 lines each and share the one thing that actually matters,
the SIGINT -> SIGTERM -> SIGKILL escalation, whose ordering is documented in
:func:`_terminate` and mirrored in `tara_cli.process.Supervisor.shutdown`.

**Process groups.** Each spawned process gets ``start_new_session=True``, so
it leads its own group and the PX4/Gazebo children that `launch_fleet.sh`
forks inherit that group. Signalling the *group* is what makes stop reach
grandchildren the manager never directly spawned — killing just the shell's
pid would orphan every `px4` and `gz sim` process it started. This is also why
:meth:`SimManager.stop` runs `stop_fleet.sh` as a backstop afterwards: a
process that calls ``setsid()`` itself escapes the group, and PX4's own
tooling has been known to.

**Platform.** The PX4 path is POSIX-only — `launch_fleet.sh` is bash and
neither PX4 SITL nor Gazebo runs natively on Windows — and is refused with a
clear error there rather than a confusing spawn failure (see
:attr:`SimManager.supported`). The ``backend="sim"`` path spawns no subprocess
at all, so it stays available on every platform: a Windows operator can still
launch and fly a fleet, just not a Gazebo-backed one.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("TaramandalGCS.sim")

#: Grace period after SIGINT before escalating to SIGTERM. PX4 and Gazebo both
#: handle SIGINT as "shut down cleanly and flush logs", which is worth waiting
#: for: a SIGKILLed Gazebo leaves lock files in ~/.gazebo/sys that make the
#: *next* launch fail, which is the failure mode an operator is least equipped
#: to diagnose.
SIGINT_GRACE_S = 5.0

#: Additional grace after SIGTERM before SIGKILL.
SIGTERM_GRACE_S = 5.0

#: Fleet sizes the dashboard's slider offers. 5 and 100 are real; 500 is
#: accepted but only meaningful on the "sim" backend — see
#: :func:`validate_drone_count`, which rejects the large ones for "px4" rather
#: than letting an operator start 500 PX4 processes and wedge the machine.
DRONE_COUNT_PRESETS = (5, 100, 500)

#: Hard ceiling for real PX4 SITL, matching the MAX_DRONES default in
#: sim/px4/launch_fleet.sh. Above roughly this many real SITL instances a
#: single machine thrashes; docs/fleet-scale-benchmark.md has the measurements.
PX4_MAX_DRONES = 50

#: Where per-process stdout/stderr goes. One file per launch, kept so the
#: operator can retrieve a failed launch's output from the dashboard instead of
#: needing shell access to the host.
LOG_DIR_NAME = "sim_logs"


class SimLaunchError(RuntimeError):
    """A launch could not be started — bad request, missing tool, or already running."""


def validate_drone_count(count: int, backend: str) -> None:
    """Raise :class:`SimLaunchError` if ``count`` drones is not launchable on ``backend``.

    The ceiling is backend-specific on purpose. The vectorized simulator is one
    process regardless of fleet size, so its limit is `tara_core.MAX_FLEET_SIZE`.
    Real PX4 SITL is one process plus a physics solve per drone, so its limit is
    much lower and enforcing it here is the difference between a rejected
    request and an unresponsive host.

    Args:
        count: Requested fleet size.
        backend: "px4" or "sim".

    Raises:
        SimLaunchError: If the count is non-positive or above the backend's ceiling.
    """
    from tara_core import MAX_FLEET_SIZE

    if count < 1:
        raise SimLaunchError(f"drone count must be at least 1, got {count}")

    if backend == "px4":
        if count > PX4_MAX_DRONES:
            raise SimLaunchError(
                f"{count} drones exceeds the real-SITL ceiling of {PX4_MAX_DRONES}. "
                'Use backend="sim" for fleets this size — see docs/fleet-scale-benchmark.md.'
            )
    elif backend == "sim":
        if count > MAX_FLEET_SIZE:
            raise SimLaunchError(
                f"{count} drones exceeds MAX_FLEET_SIZE ({MAX_FLEET_SIZE})"
            )
    else:
        raise SimLaunchError(f"unknown backend {backend!r}, expected 'px4' or 'sim'")


@dataclass
class ManagedProcess:
    """One process the manager spawned and is responsible for killing.

    Attributes:
        name: Short label ("fleet", "rviz") used as the handle in the API.
        process: The asyncio subprocess handle.
        command: Argv as spawned, echoed back in status for debuggability.
        started_at: Unix timestamp of the spawn, for an uptime readout.
        log_path: File its stdout/stderr was redirected to.
    """

    name: str
    process: asyncio.subprocess.Process
    command: list[str]
    started_at: float
    log_path: Path | None = None

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def running(self) -> bool:
        return self.process.returncode is None


@dataclass
class SimManager:
    """Owns every simulation process the GCS started.

    One instance is held by `tara_gcs.main` for the lifetime of the API
    process. It is not re-entrant: :meth:`launch` refuses to start a second
    fleet while one is running, since two PX4 fleets on one machine collide on
    both MAVLink ports and the Gazebo partition.

    Attributes:
        repo_root: Repository root, used to resolve the sim scripts.
        processes: Live processes by name.
    """

    repo_root: Path
    processes: dict[str, ManagedProcess] = field(default_factory=dict)
    _last_launch: dict | None = field(default=None, repr=False)

    #: Whether this platform can run the simulation stack at all. The scripts
    #: are bash and the payload is PX4/Gazebo; on Windows the honest answer is
    #: "not supported", surfaced as a 501 rather than a confusing spawn error.
    supported: bool = field(default_factory=lambda: sys.platform != "win32")

    @property
    def log_dir(self) -> Path:
        return self.repo_root / LOG_DIR_NAME

    # --- lifecycle ---

    async def launch(
        self,
        *,
        num_drones: int,
        backend: str = "px4",
        headless: bool = True,
    ) -> dict:
        """Spawn the simulation fleet.

        For ``backend="px4"`` this runs `sim/px4/launch_fleet.sh`, which brings
        up Gazebo and one PX4 SITL instance per drone. For ``backend="sim"``
        there is nothing to spawn at all — the vectorized simulator runs
        in-process inside this same API, driven by `DroneManager`, so this
        records the request and reports success without a subprocess. Both
        paths are launchable from the same dashboard button; the difference is
        an implementation detail the operator should not have to know.

        Args:
            num_drones: Fleet size.
            backend: "px4" for real SITL + Gazebo, "sim" for the vectorized fleet.
            headless: Run Gazebo without its GUI window. Ignored for "sim".

        Returns:
            A status dict, same shape as :meth:`status`.

        Raises:
            SimLaunchError: If unsupported, already running, misconfigured, or
                the drone count is out of range for the backend.
        """
        validate_drone_count(num_drones, backend)

        if self.is_running:
            raise SimLaunchError(
                "a simulation is already running — stop it before launching another "
                "(two PX4 fleets collide on MAVLink ports and the Gazebo partition)"
            )

        self._last_launch = {
            "num_drones": num_drones,
            "backend": backend,
            "headless": headless,
            "requested_at": time.time(),
        }

        if backend == "sim":
            # Nothing to spawn: tara_sim's fleet lives in this process. Recording
            # the request is what makes the dashboard's status pill correct.
            logger.info(
                "Sim backend requested for %d drones — vectorized fleet runs in-process, "
                "no subprocess spawned.",
                num_drones,
            )
            return self.status()

        # Only the PX4 path needs a POSIX host: it spawns a bash script that
        # drives PX4 SITL and Gazebo. The sim backend returned above without
        # spawning anything, so it stays available everywhere — which is what
        # lets a Windows operator still run a fleet, just not a Gazebo one.
        if not self.supported:
            raise SimLaunchError(
                "PX4/Gazebo launch is POSIX-only (they do not run natively on "
                f'{sys.platform}) — use backend="sim" for the vectorized fleet'
            )

        script = self.repo_root / "sim" / "px4" / "launch_fleet.sh"
        if not script.exists():
            raise SimLaunchError(f"fleet launch script not found at {script}")
        if shutil.which("bash") is None:
            raise SimLaunchError("bash not found on PATH — required to run the fleet script")

        command = ["bash", str(script), "-n", str(num_drones)]
        if headless:
            command.append("-h")

        # PX4's startup shells out to `gz`. With ROS 2 sourced, ROS's vendored
        # `gz` shadows the real Gazebo Sim binary and PX4 hangs waiting for a
        # world that never comes up. Same workaround as tara_cli.services.
        env = {**os.environ, "PATH": f"/usr/bin:{os.environ.get('PATH', '')}"}

        await self._spawn("fleet", command, env=env)
        return self.status()

    async def launch_rviz(self, *, world: str = "drone_show_field") -> dict:
        """Spawn the ROS 2 TF bridge and RViz on the running Gazebo world.

        Args:
            world: Gazebo world name to bridge poses from.

        Returns:
            A status dict.

        Raises:
            SimLaunchError: If unsupported, already running, or no PX4 fleet is
                up — RViz visualizes real Gazebo TF, so there is nothing to show
                without one.
        """
        if "rviz" in self.processes and self.processes["rviz"].running:
            raise SimLaunchError("RViz is already running")

        # Checked before the platform gate on purpose: with no fleet up, "launch
        # the fleet first" is the actionable message on every host, whereas
        # leading with "POSIX-only" would tell a Windows operator to fix the one
        # thing they cannot fix while hiding the step they actually skipped.
        if "fleet" not in self.processes or not self.processes["fleet"].running:
            raise SimLaunchError(
                "no PX4 fleet is running — RViz visualizes Gazebo TF, so launch the "
                "Gazebo SITL fleet first"
            )

        if not self.supported:
            raise SimLaunchError(f"RViz launch is POSIX-only (not available on {sys.platform})")

        script = self.repo_root / "sim" / "ros" / "launch_rviz.sh"
        if not script.exists():
            raise SimLaunchError(f"RViz launch script not found at {script}")

        await self._spawn("rviz", ["bash", str(script), "-w", world])
        return self.status()

    async def stop(self) -> dict:
        """Terminate every managed process, then sweep for orphans.

        Two phases, both necessary. First each managed process group gets the
        SIGINT -> SIGTERM -> SIGKILL escalation. Then `stop_fleet.sh` runs as a
        backstop for anything that escaped its group by calling ``setsid()``
        itself — without it, a hard-killed launch script can leave `px4` and
        `gz sim` processes alive, and the *next* launch fails on a port and
        partition collision that looks nothing like its actual cause.

        Returns:
            A dict with the stopped process names and the resulting status.
        """
        stopped = []
        for name, managed in list(self.processes.items()):
            if managed.running:
                await _terminate(managed)
                stopped.append({"name": name, "pid": managed.pid})
            self.processes.pop(name, None)

        # Backstop sweep. Best-effort: a missing script or a non-zero exit here
        # must not turn a successful stop into a failed request.
        if self.supported:
            script = self.repo_root / "sim" / "px4" / "stop_fleet.sh"
            if script.exists() and shutil.which("bash"):
                try:
                    sweep = await asyncio.create_subprocess_exec(
                        "bash", str(script),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(sweep.wait(), timeout=15.0)
                except (asyncio.TimeoutError, OSError) as exc:
                    logger.warning("stop_fleet.sh sweep did not complete cleanly: %s", exc)

        self._last_launch = None
        logger.info("Simulation stopped — %d process(es) terminated.", len(stopped))
        return {"stopped": stopped, "status": self.status()}

    # --- introspection ---

    @property
    def is_running(self) -> bool:
        """True if any managed process is alive, or an in-process sim fleet was requested."""
        if any(p.running for p in self.processes.values()):
            return True
        return bool(self._last_launch and self._last_launch["backend"] == "sim")

    def status(self) -> dict:
        """Report what's running, with per-process resource usage.

        Reaps any process that exited since the last call, so a crashed fleet
        stops being reported as RUNNING. CPU/RSS come from psutil when it's
        installed (it is, for `tools/benchmark_fleet_scale.py`) and are simply
        omitted when it isn't — a resource readout is a nice-to-have, and
        making the whole status endpoint fail without it would not be.

        Returns:
            A dict the dashboard's status pills render directly.
        """
        for name, managed in list(self.processes.items()):
            if not managed.running:
                logger.info(
                    "Process '%s' (pid %d) exited with code %s.",
                    name, managed.pid, managed.process.returncode,
                )
                self.processes.pop(name, None)

        procs = [self._describe(m) for m in self.processes.values()]
        launch = self._last_launch or {}
        num_drones = launch.get("num_drones", 0)
        backend = launch.get("backend")

        return {
            "running": self.is_running,
            "supported": self.supported,
            "backend": backend,
            "num_drones": num_drones,
            # One simulated MAVLink port per drone, matching the port block
            # tara_gcs.main derives its fleet from. Zero for the sim backend,
            # which has no network layer at all.
            "mavlink_ports": (
                [14540 + i for i in range(num_drones)] if backend == "px4" else []
            ),
            "processes": procs,
            "cpu_percent": round(sum(p.get("cpu_percent") or 0.0 for p in procs), 1),
            "memory_mb": round(sum(p.get("memory_mb") or 0.0 for p in procs), 1),
            "rviz_running": "rviz" in self.processes,
        }

    # --- internals ---

    async def _spawn(self, name: str, command: list[str], *, env: dict | None = None) -> ManagedProcess:
        """Spawn one process in its own session, with output redirected to a log file."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{name}.log"

        logger.info("Spawning '%s': %s", name, " ".join(command))
        # The log file handle is intentionally not held open on the manager:
        # the child inherits the fd, so closing it here leaves the child
        # writing happily while the manager holds no resource it must remember
        # to release on an error path.
        with open(log_path, "wb") as log_file:
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(self.repo_root),
                    env=env,
                    stdout=log_file,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as exc:
                raise SimLaunchError(f"failed to spawn '{name}': {exc}") from exc

        managed = ManagedProcess(
            name=name,
            process=process,
            command=command,
            started_at=time.time(),
            log_path=log_path,
        )
        self.processes[name] = managed
        return managed

    @staticmethod
    def _describe(managed: ManagedProcess) -> dict:
        """Build one process's status entry, including psutil metrics when available."""
        entry = {
            "name": managed.name,
            "pid": managed.pid,
            "running": managed.running,
            "uptime_s": round(time.time() - managed.started_at, 1),
            "command": managed.command,
            "log_path": str(managed.log_path) if managed.log_path else None,
            "cpu_percent": None,
            "memory_mb": None,
            "child_count": None,
        }

        try:
            import psutil
        except ImportError:
            return entry

        try:
            proc = psutil.Process(managed.pid)
            children = proc.children(recursive=True)
            # The launcher shell itself uses no CPU; the PX4 and Gazebo
            # children it forked use all of it. Summing the tree is the only
            # number that means anything to an operator watching load.
            tree = [proc, *children]
            cpu = 0.0
            rss = 0
            for p in tree:
                try:
                    cpu += p.cpu_percent(interval=None)
                    rss += p.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            entry["cpu_percent"] = round(cpu, 1)
            entry["memory_mb"] = round(rss / (1024 * 1024), 1)
            entry["child_count"] = len(children)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

        return entry


async def _terminate(managed: ManagedProcess) -> None:
    """Stop one process group with SIGINT -> SIGTERM -> SIGKILL escalation.

    The order is deliberate and matters most at the first step: PX4 and Gazebo
    treat SIGINT as a clean shutdown and release their lock files, while a
    SIGKILLed Gazebo leaves ~/.gazebo/sys locks behind that break the next
    launch. So the polite signal goes first and gets a real grace period; the
    harsher ones exist only so a wedged process cannot leave the fleet
    un-stoppable from the dashboard.

    Signals go to the process *group* (``killpg``), not the pid, so the PX4 and
    Gazebo children forked by the launcher shell are included. If the group is
    already gone, ``ProcessLookupError`` means the work is done.
    """
    pid = managed.pid
    for sig, grace in (
        (signal.SIGINT, SIGINT_GRACE_S),
        (signal.SIGTERM, SIGTERM_GRACE_S),
        (signal.SIGKILL, 2.0),
    ):
        if not managed.running:
            return
        try:
            os.killpg(os.getpgid(pid), sig)
            logger.info("Sent %s to process group of '%s' (pid %d).", sig.name, managed.name, pid)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            logger.error("Not permitted to signal '%s' (pid %d): %s", managed.name, pid, exc)
            return

        try:
            await asyncio.wait_for(managed.process.wait(), timeout=grace)
            logger.info("'%s' (pid %d) exited after %s.", managed.name, pid, sig.name)
            return
        except asyncio.TimeoutError:
            logger.warning(
                "'%s' (pid %d) still alive %.0fs after %s — escalating.",
                managed.name, pid, grace, sig.name,
            )

    logger.error("'%s' (pid %d) survived SIGKILL.", managed.name, pid)
