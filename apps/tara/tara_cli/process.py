"""Runs a set of services as one supervised unit.

Each service is spawned with ``start_new_session=True``, which makes it the
leader of its own new process group (pgid == its own pid). A bash script like
``sim/px4/launch_fleet.sh`` that goes on to spawn PX4/Gazebo children inherits
that group, so killing the group catches the whole tree — including
grandchildren the supervisor never directly saw.

An literal single shared process group for every service (the more obvious
reading of "one process group") was considered and rejected: any child that
calls ``setsid()`` itself (a few daemonizing tools do) would silently escape a
shared group, whereas giving each service its own group and having the
supervisor fan a signal out to all of them is both simpler to reason about and
no less thorough — one Ctrl+C still reaches everything.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

_COLORS = [36, 35, 32, 33, 34, 31, 96, 95]  # ANSI foreground codes, cycled per service
_RESET = "\033[0m"

#: Grace period between SIGTERM and SIGKILL when a service won't stop.
SHUTDOWN_GRACE_S = 5.0


@dataclass
class Service:
    """One long-running process the supervisor manages.

    Attributes:
        name: Short label shown as a log-line prefix.
        command: Argv to execute.
        cwd: Working directory, defaults to the current directory.
        env: Extra environment variables layered on top of the current
            environment.
    """

    name: str
    command: list[str]
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)

    process: subprocess.Popen | None = field(default=None, repr=False, compare=False)


class Supervisor:
    """Starts, streams, and cleanly tears down a group of :class:`Service`."""

    def __init__(self) -> None:
        self._services: list[Service] = []
        self._threads: list[threading.Thread] = []
        self._print_lock = threading.Lock()
        self._shutting_down = False

    def add(self, service: Service, *, delay_before: float = 0.0) -> None:
        """Start ``service`` immediately and begin streaming its output.

        Args:
            service: The service to launch.
            delay_before: Seconds to sleep before spawning, so a dependent
                service (e.g. the web app) doesn't race its backend's startup.
        """
        if delay_before:
            time.sleep(delay_before)

        color = _COLORS[len(self._services) % len(_COLORS)]
        prefix = f"\033[{color}m[{service.name}]{_RESET} "

        full_env = {**os.environ, **service.env}
        service.process = subprocess.Popen(
            service.command,
            cwd=service.cwd,
            env=full_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self._services.append(service)

        thread = threading.Thread(
            target=self._pump_output, args=(service, prefix), daemon=True
        )
        thread.start()
        self._threads.append(thread)

    def _pump_output(self, service: Service, prefix: str) -> None:
        assert service.process is not None and service.process.stdout is not None
        try:
            for line in service.process.stdout:
                with self._print_lock:
                    sys.stdout.write(prefix + line)
                    sys.stdout.flush()
        except ValueError:
            pass  # stream closed under us during shutdown; nothing left to read

    def run_until_exit_or_signal(self) -> int:
        """Block until a service dies unexpectedly or the process receives a signal.

        Returns:
            0 on a clean Ctrl+C shutdown, 1 if a service exited on its own.
        """
        stop_requested = threading.Event()

        def _handle_signal(signum, _frame):
            with self._print_lock:
                sys.stdout.write(f"\n[tara] received {signal.Signals(signum).name}, shutting down...\n")
                sys.stdout.flush()
            stop_requested.set()

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        exit_code = 0
        try:
            while not stop_requested.is_set():
                for service in self._services:
                    rc = service.process.poll()
                    if rc is not None:
                        with self._print_lock:
                            sys.stdout.write(
                                f"[tara] '{service.name}' exited unexpectedly "
                                f"(code {rc}) — stopping the rest of the stack.\n"
                            )
                            sys.stdout.flush()
                        exit_code = 1
                        stop_requested.set()
                        break
                time.sleep(0.5)
        finally:
            self.shutdown()
        return exit_code

    def shutdown(self) -> None:
        """Terminate every service's process group, escalating to SIGKILL."""
        if self._shutting_down:
            return
        self._shutting_down = True

        for service in reversed(self._services):
            proc = service.process
            if proc is None or proc.poll() is not None:
                continue
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                continue

        deadline = time.monotonic() + SHUTDOWN_GRACE_S
        for service in reversed(self._services):
            proc = service.process
            if proc is None:
                continue
            remaining = max(0.0, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=2.0)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass

        for thread in self._threads:
            thread.join(timeout=1.0)
