"""Runs the black-box recorder inside the GCS (issue #65).

``BlackboxService`` is the glue between :mod:`tara_gcs.blackbox` and a running
``DroneManager``. It touches the drone manager only through its public
attributes, so the recorder needs no hook inside the flight code:

* a 10 Hz task samples ``telemetry`` (+ ``rf_channel_drop_rate()``);
* the fleet FSM and the audit logs call it as things happen
  (``FleetStateMachine.observers`` / ``AuditLog.observers``), so a command or a
  lifecycle transition is on disk before the next tick, not at the next poll;
* the proximity and geofence breach dicts are diffed at 20 Hz, twice the
  monitors' own rate, so a breach is not missed between two polls.

On by default (``GCS_BLACKBOX=0`` turns it off): evidence of an incident cannot
be recovered afterwards, so a recorder that has to be remembered is absent on
exactly the day it is needed. It costs ~0.5 ms per tick and ~3 MB per
15 minutes at 100 drones.

Disk use is bounded: a session file rotates at ``file_mb`` and the oldest
session files are deleted once the directory exceeds ``max_total_mb``. The
file in use is never deleted.

Nothing here may disturb flight. Every entry point catches and logs its own
errors; the recorder itself swallows disk errors (see ``BlackboxRecorder``);
a recorder that has failed is retried with a fresh file once a minute.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import math
import os
from typing import Any, Callable

from tara_gcs.blackbox import DEFAULT_DIR, BlackboxRecorder

logger = logging.getLogger("TaramandalGCS")

SAMPLE_HZ = 10.0
#: Breach / FSM polling period: twice the monitors' 10 Hz.
POLL_S = 0.05
RETRY_AFTER_FAILURE_S = 60.0
_SESSION_GLOB = "blackbox_*.jsonl.gz"


def prune_sessions(directory: str, max_total_bytes: int, keep: str | None = None) -> list[str]:
    """Delete the oldest session files until the directory fits ``max_total_bytes``.

    ``keep`` (the file being written) is never deleted. Returns what was removed.
    """
    files = []
    for path in glob.glob(os.path.join(directory, _SESSION_GLOB)):
        try:
            st = os.stat(path)
        except OSError:
            continue
        files.append((st.st_mtime, path, st.st_size))
    files.sort()
    total = sum(size for _, _, size in files)
    removed = []
    for _, path, size in files:
        if total <= max_total_bytes:
            break
        if keep is not None and os.path.abspath(path) == os.path.abspath(keep):
            continue
        try:
            os.remove(path)
        except OSError:
            continue
        total -= size
        removed.append(path)
    return removed


class BlackboxService:
    """Owns the recorder session(s) for one GCS process.

    Args:
        drone_manager: Duck-typed: ``telemetry``, ``fleet_fsm``,
            ``proximity_breaches``, ``geofence_breaches``,
            ``geofence_warnings``, ``geofence_radius``,
            ``soft_geofence_radius`` and ``rf_channel_drop_rate()``.
        audit_logs: ``AuditLog`` instances whose entries are recorded as
            command / audit events.
        directory: Where session files go.
        max_total_mb: Cap on the directory's total size.
        file_mb: Rotate the session file at this size (clamped to a quarter
            of ``max_total_mb`` so pruning always has older files to drop).
        context: Stamped on the header and every event (backend / fidelity).
        enabled: ``False`` makes every method a no-op.
    """

    def __init__(
        self,
        drone_manager: Any,
        *,
        audit_logs: tuple = (),
        directory: str = DEFAULT_DIR,
        max_total_mb: float = 500.0,
        file_mb: float = 100.0,
        context: Callable[[], dict] | None = None,
        enabled: bool = True,
        recorder_kwargs: dict | None = None,
    ):
        self.dm = drone_manager
        self.audit_logs = audit_logs
        self.directory = directory
        self.max_total_bytes = int(max_total_mb * 1024 * 1024)
        self.file_bytes = int(min(file_mb, max_total_mb / 4) * 1024 * 1024)
        self.context = context
        self.enabled = enabled
        self._recorder_kwargs = recorder_kwargs or {}

        self._rec: BlackboxRecorder | None = None
        self._task: asyncio.Task | None = None
        #: Finishing a rotated-out session (close + prune) runs off the loop; stop()
        #: waits for these, so cancelling the sampler mid-rotation cannot leave a
        #: session file without its gzip trailer.
        self._finishing: set[asyncio.Task] = set()
        self._next_sample = 0.0
        self._retry_at = 0.0
        self._errors = 0
        self.sessions = 0
        self._prox: set[tuple[int, int]] = set()
        self._hard: set[int] = set()
        self._soft: set[int] = set()

    # ------------------------------------------------------------- lifecycle

    @property
    def recorder(self) -> BlackboxRecorder | None:
        return self._rec

    def start(self) -> None:
        """Open the first session and start the sampling task. Never raises."""
        if not self.enabled or self._task is not None:
            return
        try:
            self._open_session()
        except Exception:
            logger.exception("Blackbox: could not open a session in %s; the GCS runs without a flight recorder", self.directory)
            return
        for log in self.audit_logs:
            log.observers.append(self._on_audit)
        self.dm.fleet_fsm.observers.append(self._on_fsm)
        self._task = asyncio.create_task(self._run(), name="blackbox")

    async def stop(self) -> None:
        """Stop sampling and close the file (gzip trailer + fsync). Never raises."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for log in self.audit_logs:
            if self._on_audit in log.observers:
                log.observers.remove(self._on_audit)
        fsm = getattr(self.dm, "fleet_fsm", None)
        if fsm is not None and self._on_fsm in fsm.observers:
            fsm.observers.remove(self._on_fsm)
        if self._finishing:
            await asyncio.gather(*self._finishing, return_exceptions=True)
        rec, self._rec = self._rec, None
        if rec is not None:
            try:
                await asyncio.get_running_loop().run_in_executor(None, rec.close)
            except Exception:
                logger.exception("Blackbox: error closing %s", rec.path)

    def status(self) -> dict:
        rec = self._rec
        return {
            "enabled": self.enabled,
            "recording": rec is not None and not rec.failed and not rec.closed,
            "directory": self.directory,
            "path": rec.path if rec else None,
            "sessions": self.sessions,
            "ticks": rec.ticks if rec else 0,
            "events": rec.events if rec else 0,
            "dropped": rec.dropped if rec else 0,
            "errors": rec.errors if rec else 0,
            "failed": rec.failed if rec else False,
            "bytes_written": rec.bytes_written if rec else 0,
            "max_total_mb": self.max_total_bytes / 1024 / 1024,
            "file_mb": self.file_bytes / 1024 / 1024,
        }

    # --------------------------------------------------------------- session

    def _open_session(self) -> None:
        old_files = prune_sessions(self.directory, self.max_total_bytes - self.file_bytes) if os.path.isdir(self.directory) else []
        if old_files:
            logger.info("Blackbox: removed %d old session file(s) to stay under the %.0f MB cap", len(old_files), self.max_total_bytes / 1048576)
        self._rec = BlackboxRecorder.new_session(self.directory, context=self.context, **self._recorder_kwargs)
        self.sessions += 1
        self._prox, self._hard, self._soft = set(), set(), set()  # a new file starts from "no breach"
        logger.info("Blackbox: recording to %s", self._rec.path)

    async def _rotate(self) -> None:
        old = self._rec
        try:
            self._open_session()
        except Exception:
            logger.exception("Blackbox: could not open a new session; keeping the current one")
            self._rec = old
            return
        if old is not None:
            finishing = asyncio.ensure_future(self._finish_session(old, keep=self._rec.path))
            self._finishing.add(finishing)
            finishing.add_done_callback(self._finishing.discard)
            await asyncio.shield(finishing)  # a cancel lands here, not inside the close

    async def _finish_session(self, old: BlackboxRecorder, keep: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, old.close)
            await loop.run_in_executor(None, prune_sessions, self.directory, self.max_total_bytes, keep)
        except Exception:
            logger.exception("Blackbox: error finishing %s", old.path)

    # ------------------------------------------------------------- observers

    def _on_fsm(self, entry: dict) -> None:
        rec = self._rec
        if rec is None:
            return
        # The safety monitors set a breach flag and then trigger the failsafe
        # transition in the same step, so by the time this runs the flag is
        # already up. Sweep first, so the log reads cause (breach) before
        # effect (RTL) instead of whenever the next 20 Hz poll gets there.
        try:
            self._poll_breaches(rec)
        except Exception:
            logger.exception("Blackbox: breach sweep failed")
        rec.record_fsm_transition(entry)

    def _on_audit(self, entry: dict) -> None:
        rec = self._rec
        if rec is None:
            return
        fields = {k: v for k, v in entry.items() if k != "ts"}
        event = fields.pop("event", None)
        if event == "command":  # an API call, as audit_commands records it
            method, path, status = fields.pop("method", ""), fields.pop("path", ""), fields.pop("status", 0)
            rec.record_event("command", command=path, trigger=f"{method} {path}", accepted=status < 400, status=status, **fields)
        else:
            rec.record_event("audit", audit_event=event, **fields)

    # -------------------------------------------------------------- the task

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while True:
                try:
                    await self._poll(loop.time())
                except Exception:
                    self._errors += 1
                    if self._errors in (1, 10, 100) or self._errors % 1000 == 0:
                        logger.exception("Blackbox: sampling error #%d (flight is unaffected)", self._errors)
                await asyncio.sleep(POLL_S)
        except asyncio.CancelledError:
            pass

    async def _poll(self, now: float) -> None:
        rec = self._rec
        if rec is None:
            return
        if rec.failed:
            if now >= self._retry_at:
                self._retry_at = now + RETRY_AFTER_FAILURE_S
                logger.warning("Blackbox: recorder had failed; trying a fresh session file")
                await self._rotate()
            return
        self._poll_breaches(rec)
        if now >= self._next_sample:
            self._next_sample = max(self._next_sample + 1.0 / SAMPLE_HZ, now - 1.0 / SAMPLE_HZ)
            rec.record_tick(self.dm.telemetry, rf_drop_rate=self.dm.rf_channel_drop_rate())
        if rec.bytes_written >= self.file_bytes:
            await self._rotate()

    def _poll_breaches(self, rec: BlackboxRecorder) -> None:
        dm = self.dm
        tel = dm.telemetry

        prox = {pair for pair, active in dm.proximity_breaches.items() if active}
        for pair in sorted(prox - self._prox):
            rec.record_proximity_breach(pair, self._pair_distance(tel, pair))
        for pair in sorted(self._prox - prox):
            rec.record_proximity_breach(pair, self._pair_distance(tel, pair), cleared=True)
        self._prox = prox

        for level, flags, limit, attr in (
            ("hard", dm.geofence_breaches, getattr(dm, "geofence_radius", None), "_hard"),
            ("soft", dm.geofence_warnings, getattr(dm, "soft_geofence_radius", None), "_soft"),
        ):
            active = {d for d, on in flags.items() if on}
            before: set[int] = getattr(self, attr)
            for drone in sorted(active - before):
                rec.record_geofence(drone, level, self._home_distance(tel, drone), limit)
            for drone in sorted(before - active):
                rec.record_geofence(drone, level, self._home_distance(tel, drone), limit, cleared=True)
            setattr(self, attr, active)

    @staticmethod
    def _pair_distance(tel: dict, pair: tuple[int, int]) -> float:
        try:
            a, b = tel[pair[0]], tel[pair[1]]
            return math.dist((a["fleet_x"], a["fleet_y"], a["fleet_z"]), (b["fleet_x"], b["fleet_y"], b["fleet_z"]))
        except (KeyError, TypeError):
            return math.nan

    @staticmethod
    def _home_distance(tel: dict, drone: int) -> float:
        try:
            return math.hypot(tel[drone]["local_x"], tel[drone]["local_y"])
        except (KeyError, TypeError):
            return math.nan
