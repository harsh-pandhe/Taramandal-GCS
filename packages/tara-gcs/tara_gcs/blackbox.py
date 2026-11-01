"""Black-box flight recorder: a 10 Hz gzip JSONL log of everything needed to
reconstruct an incident afterwards (issue #65, V1 slice).

What is recorded
----------------
* **Telemetry vectors**, one row per tick for the whole fleet: fleet-frame
  position, heading, battery, GNSS fix type and satellites, flight mode, link
  latency and the RF packet-drop rate, plus a flag word (connected / armed /
  aborted / plan valid / GPS lock). Velocity is not a separate channel: the
  GCS telemetry dict does not carry it, and it is the position difference over
  the (recorded) tick time.
* **Events**: GCS command invocations, fleet FSM transitions and rejections,
  proximity and geofence breaches. Events are rare and are the part an
  investigator reads first, so each one is flushed to disk as it is recorded.

V1 is evidence preservation, not a replay UI: the file is self-describing and
:func:`read_blackbox` decodes it, but nothing here renders anything.

File format (``*.jsonl.gz``, one JSON object per line)
------------------------------------------------------
``header``  once: format version, rate, wall-clock start, channel table
            (source field, scale, unit) and the caller's context
            (backend / fidelity, as the audit logs stamp since issue #170).
``key``     every ``keyframe_interval_s``, and whenever the drone set or a
            string dictionary changes: absolute values for every channel plus
            the id list and the dictionaries.
``tick``    every sampled tick: per-channel first differences against the
            previous tick, so steady flight codes as small numbers. (Second
            differences for position were measured on the real sim and saved
            ~7%: sensor noise dominates, so the extra state is not worth it.)
``event``   sparse, free-form, ``kind`` + fields.

Values are quantised integers (``scale`` per channel), which is what keeps
100 drones for 15 minutes in single-digit megabytes: the numbers are small and
repetitive, so deflate does the rest.

Durability
----------
The compressor is sync-flushed and the file flushed to the OS every
``flush_interval_s`` (default 1 s) and after every event, so a process that is
SIGKILLed loses at most the last second of *ticks* and no event. A background
thread fsyncs every ``fsync_interval_s`` for the power-loss case. A killed
recorder leaves a gzip stream with no trailer; :func:`read_blackbox` reads it
anyway and reports ``truncated=True``.

A recorder must never take the aircraft down: a full disk, a bad telemetry
value or a closed file is logged once and counted, never raised into the
10 Hz safety loops that call it.

Wiring
------
Not wired in yet (``drone_manager.py`` / ``main.py`` are being changed by other
work in flight): the intended call sites are ``record_tick`` from the sim bulk
loop / a 10 Hz task, ``record_command`` next to ``command_audit.record``,
``record_fsm_transition`` from the FSM's audit hook, and
``record_proximity_breach`` / ``record_geofence`` from the two monitor loops.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Mapping, Sequence

import numpy as np

logger = logging.getLogger("TaramandalGCS")

FORMAT_NAME = "tara-blackbox"
FORMAT_VERSION = 1

DEFAULT_DIR = os.path.join(os.path.dirname(__file__), "audit", "blackbox")

#: Bits of the ``s`` flag channel, from the telemetry dict's fields.
FLAG_BITS: dict[str, int] = {"connected": 0, "armed": 1, "aborted": 2, "planned_valid": 3, "gps_lock": 4}

#: Value recorded for a channel the source did not provide (no RF model, no
#: latency probe). Negative so it cannot be mistaken for a real reading.
MISSING = -1


@dataclass(frozen=True)
class Channel:
    key: str
    source: str  # telemetry dict field
    scale: float  # stored int = round(value * scale)
    unit: str


#: Numeric channels, in line order.
CHANNELS: tuple[Channel, ...] = (
    Channel("x", "fleet_x", 100, "cm"),
    Channel("y", "fleet_y", 100, "cm"),
    Channel("z", "fleet_z", 100, "cm"),
    Channel("h", "heading", 10, "0.1deg"),
    Channel("b", "battery_percent", 10, "0.1%"),
    Channel("v", "battery_voltage", 10, "0.1V"),
    Channel("n", "satellites", 1, "count"),
    Channel("l", "comm_latency_ms", 1, "ms"),
)
#: Channels filled from outside the telemetry dict / derived from several fields.
_RF_KEY = "r"  # RF drop rate, 1e-4 units
_FLAG_KEY = "s"
_MODE_KEY = "m"  # index into the key line's "modes" dictionary
_FIX_KEY = "f"  # index into "fixes"
_ALL_KEYS = tuple(c.key for c in CHANNELS) + (_RF_KEY, _FLAG_KEY, _MODE_KEY, _FIX_KEY)


def _utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds")


def _header_channels() -> list[dict]:
    rows = [{"key": c.key, "source": c.source, "scale": c.scale, "unit": c.unit} for c in CHANNELS]
    rows += [
        {"key": _RF_KEY, "source": "rf_drop_rate", "scale": 10000, "unit": "1e-4", "missing": MISSING},
        {"key": _FLAG_KEY, "source": "flags", "scale": 1, "unit": "bits"},
        {"key": _MODE_KEY, "source": "flight_mode", "scale": 1, "unit": "dict:modes"},
        {"key": _FIX_KEY, "source": "gps_fix_type", "scale": 1, "unit": "dict:fixes"},
    ]
    return rows


class BlackboxRecorder:
    """Append-only recorder for one session. Single-threaded: call it from the
    GCS event loop only (the fsync thread touches nothing but the file
    descriptor).

    Args:
        path: Output file. Created exclusively (never overwrites evidence).
        rate_hz: Nominal tick rate; ticks arriving faster than half a period
            apart are dropped, so a caller that is invoked by several loops
            cannot double the rate.
        keyframe_interval_s: Spacing of absolute-value key lines.
        flush_interval_s: Sync-flush + OS flush cadence for ticks.
        fsync_interval_s: Cadence of the background fsync, or ``None`` to
            disable it.
        compresslevel: zlib level, 1-9.
        context: Called for the header and every event, merged in (explicit
            event fields win), same pattern as ``preflight.AuditLog``.
        clock / wall_clock: Injectable for tests.
    """

    def __init__(
        self,
        path: str,
        *,
        rate_hz: float = 10.0,
        keyframe_interval_s: float = 10.0,
        flush_interval_s: float = 1.0,
        fsync_interval_s: float | None = 5.0,
        compresslevel: int = 6,
        context: Callable[[], dict] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ):
        self.path = path
        self.rate_hz = rate_hz
        self.keyframe_interval_s = keyframe_interval_s
        self.flush_interval_s = flush_interval_s
        self.context = context
        self._clock = clock
        self._wall = wall_clock

        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._file = open(path, "xb")  # exclusive: refuse to overwrite an earlier session
        self._comp = zlib.compressobj(compresslevel, zlib.DEFLATED, 16 + zlib.MAX_WBITS)

        self._t0 = clock()
        self._started_wall = wall_clock()
        self._period = 1.0 / rate_hz
        self._last_tick_t = -math.inf
        self._last_flush_t = self._t0
        self._last_key_t = -math.inf

        self._ids: list[int] = []
        self._prev: dict[str, np.ndarray] = {}
        self._modes: dict[str, int] = {}
        self._fixes: dict[str, int] = {}

        #: Set after a write failure; the recorder then drops everything.
        self.failed = False
        self.closed = False
        self.ticks = 0
        self.events = 0
        self.dropped = 0
        self.errors = 0
        self.bytes_written = 0

        self._dirty = False
        self._fsync_stop = threading.Event()
        self._fsync_thread: threading.Thread | None = None

        self._emit({
            "type": "header",
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "rate_hz": rate_hz,
            "keyframe_interval_s": keyframe_interval_s,
            "flush_interval_s": flush_interval_s,
            "started_wall": self._started_wall,
            "started": _utc_iso(self._started_wall),
            "pid": os.getpid(),
            "channels": _header_channels(),
            "flag_bits": FLAG_BITS,
            "context": self._context(),
        })
        self._flush()
        if fsync_interval_s:
            self._fsync_thread = threading.Thread(
                target=self._fsync_loop, args=(fsync_interval_s,), name="blackbox-fsync", daemon=True
            )
            self._fsync_thread.start()

    @classmethod
    def new_session(cls, directory: str = DEFAULT_DIR, **kwargs) -> "BlackboxRecorder":
        """A recorder writing ``blackbox_<UTC start>_<pid>.jsonl.gz`` in ``directory``."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return cls(os.path.join(directory, f"blackbox_{stamp}_{os.getpid()}.jsonl.gz"), **kwargs)

    # ------------------------------------------------------------- recording

    def record_tick(
        self,
        telemetry: Mapping[int, Mapping[str, Any]],
        *,
        rf_drop_rate: Mapping[int, float] | Sequence[float] | float | None = None,
    ) -> bool:
        """Sample the whole fleet. ``telemetry`` is ``DroneManager.telemetry``.

        ``rf_drop_rate`` is the per-drone drop rate (indexed by drone id), a
        single fleet figure applied to every drone, or ``None`` when no RF
        model is running. Returns whether a row was written.
        """
        if self.failed or self.closed:
            self.dropped += 1
            return False
        now = self._clock()
        if now - self._last_tick_t < self._period * 0.5:
            return False
        try:
            self._tick(now, telemetry, rf_drop_rate)
        except OSError as e:
            self._fail(e)
            return False
        except Exception:
            self.errors += 1
            if self.errors == 1:
                logger.exception("Blackbox: could not encode a tick; skipping it")
            return False
        self._last_tick_t = now
        return True

    def record_event(self, kind: str, **fields) -> None:
        """One event line, flushed to the OS immediately."""
        if self.failed or self.closed:
            self.dropped += 1
            return
        try:
            entry = {"type": "event", "t": self._t_ms(self._clock()), "wall": self._wall(), "kind": kind}
            entry.update(self._context())
            entry.update(fields)
            self._emit(entry)
            self.events += 1
            self._flush()
        except OSError as e:
            self._fail(e)
        except Exception:
            self.errors += 1
            logger.exception("Blackbox: could not record event %s", kind)

    def record_command(self, command: str, *, operator: str, trigger: str, accepted: bool = True, **extra) -> None:
        """A GCS command invocation (accepted or refused)."""
        self.record_event("command", command=command, operator=operator, trigger=trigger, accepted=accepted, **extra)

    def record_fsm_transition(self, record: Mapping[str, Any]) -> None:
        """A ``FleetStateMachine`` audit record: a transition or a rejected command."""
        accepted = record.get("accepted", True)
        fields = {k: v for k, v in record.items() if k not in ("ts", "time")}
        self.record_event("fsm_transition" if accepted else "fsm_rejected", **fields)

    def record_proximity_breach(self, pair: tuple[int, int], distance_m: float, *, cleared: bool = False) -> None:
        self.record_event(
            "proximity_clear" if cleared else "proximity_breach", drones=list(pair), distance_m=round(distance_m, 3)
        )

    def record_geofence(
        self, drone_id: int, level: str, distance_m: float, limit_m: float | None, *, cleared: bool = False
    ) -> None:
        """``level`` is ``"hard"`` (RTL) or ``"soft"`` (warning)."""
        self.record_event(
            f"geofence_{level}_{'clear' if cleared else 'breach'}",
            drone=drone_id,
            distance_m=round(distance_m, 3),
            limit_m=limit_m,
        )

    # --------------------------------------------------------------- control

    def flush(self) -> None:
        """Push everything recorded so far to the OS."""
        if self.failed or self.closed:
            return
        try:
            self._flush()
        except OSError as e:
            self._fail(e)

    def close(self) -> None:
        """Finish the gzip stream (trailer included) and fsync. Idempotent."""
        if self.closed:
            return
        self.closed = True
        self._fsync_stop.set()
        if self._fsync_thread is not None:
            self._fsync_thread.join(timeout=2.0)
        try:
            if not self.failed:
                self._file.write(self._comp.flush(zlib.Z_FINISH))
                self._file.flush()
                os.fsync(self._file.fileno())
        except OSError as e:
            self._fail(e)
        finally:
            try:
                self._file.close()
            except OSError:
                pass

    def __enter__(self) -> "BlackboxRecorder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -------------------------------------------------------------- internals

    def _context(self) -> dict:
        if self.context is None:
            return {}
        try:
            return dict(self.context())
        except Exception:  # a recorder must survive a bad context provider
            logger.exception("Blackbox: context provider failed; recording without it")
            return {}

    def _t_ms(self, now: float) -> int:
        return int(round((now - self._t0) * 1000))

    def _emit(self, obj: dict) -> None:
        data = (json.dumps(obj, separators=(",", ":"), default=str) + "\n").encode()
        out = self._comp.compress(data)
        if out:
            self._file.write(out)
            self.bytes_written += len(out)
        self._dirty = True

    def _flush(self) -> None:
        out = self._comp.flush(zlib.Z_SYNC_FLUSH)
        if out:
            self._file.write(out)
            self.bytes_written += len(out)
        self._file.flush()
        self._last_flush_t = self._clock()

    def _fail(self, err: Exception) -> None:
        self.dropped += 1
        if not self.failed:
            self.failed = True
            logger.error(f"Blackbox: write to {self.path} failed ({err}); recording stopped, flight is unaffected")

    def _fsync_loop(self, interval: float) -> None:
        while not self._fsync_stop.wait(interval):
            if self._dirty and not self.closed:
                self._dirty = False
                try:
                    os.fsync(self._file.fileno())
                except (OSError, ValueError):  # closed under us, or the disk failed
                    return

    def _code(self, table: dict[str, int], names: list[str]) -> list[int]:
        out = []
        for name in names:
            idx = table.get(name)
            if idx is None:
                idx = table[name] = len(table)
            out.append(idx)
        return out

    def _columns(self, telemetry, ids, rf_drop_rate) -> dict[str, np.ndarray]:
        rows = [telemetry[i] for i in ids]
        cols: dict[str, np.ndarray] = {}
        for ch in CHANNELS:
            raw = np.array([r.get(ch.source) or 0.0 for r in rows], dtype=float)
            cols[ch.key] = np.rint(np.nan_to_num(raw, nan=0.0, posinf=0.0, neginf=0.0) * ch.scale).astype(np.int64)
        if rf_drop_rate is None:
            cols[_RF_KEY] = np.full(len(ids), MISSING, dtype=np.int64)
        else:
            if isinstance(rf_drop_rate, (int, float)):
                rf = np.full(len(ids), float(rf_drop_rate))
            elif isinstance(rf_drop_rate, Mapping):
                rf = np.array([rf_drop_rate.get(i, math.nan) for i in ids], dtype=float)
            else:
                rf = np.array([rf_drop_rate[i] for i in ids], dtype=float)
            ok = np.isfinite(rf)
            cols[_RF_KEY] = np.where(ok, np.rint(np.where(ok, rf, 0.0) * 10000), MISSING).astype(np.int64)
        cols[_FLAG_KEY] = np.array(
            [
                (bool(r.get("connected")) << FLAG_BITS["connected"])
                | (bool(r.get("armed")) << FLAG_BITS["armed"])
                | (bool(r.get("aborted")) << FLAG_BITS["aborted"])
                | (bool(r.get("planned_valid")) << FLAG_BITS["planned_valid"])
                | (bool(r.get("gps_lock")) << FLAG_BITS["gps_lock"])
                for r in rows
            ],
            dtype=np.int64,
        )
        cols[_MODE_KEY] = np.array(self._code(self._modes, [str(r.get("flight_mode", "UNKNOWN")) for r in rows]), dtype=np.int64)
        cols[_FIX_KEY] = np.array(self._code(self._fixes, [str(r.get("gps_fix_type", "NO_GPS")) for r in rows]), dtype=np.int64)
        return cols

    def _tick(self, now: float, telemetry, rf_drop_rate) -> None:
        ids = sorted(telemetry)
        if not ids:
            return
        modes_before, fixes_before = len(self._modes), len(self._fixes)
        cols = self._columns(telemetry, ids, rf_drop_rate)
        t_ms = self._t_ms(now)

        need_key = (
            ids != self._ids
            or len(self._modes) != modes_before
            or len(self._fixes) != fixes_before
            or now - self._last_key_t >= self.keyframe_interval_s
        )
        if need_key:
            self._emit({
                "type": "key",
                "t": t_ms,
                "ids": ids,
                "modes": list(self._modes),
                "fixes": list(self._fixes),
                "ch": {k: cols[k].tolist() for k in _ALL_KEYS},
            })
            self._ids = ids
            self._last_key_t = now
        else:
            diff = {k: (cols[k] - self._prev[k]).tolist() for k in _ALL_KEYS}
            self._emit({"type": "tick", "t": t_ms, "ch": diff})
        self._prev = cols
        self.ticks += 1
        if now - self._last_flush_t >= self.flush_interval_s:
            self._flush()


# ------------------------------------------------------------------- reading


@dataclass
class BlackboxLog:
    header: dict
    ticks: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    #: True when the stream ended without its gzip trailer / a complete last
    #: line - what a SIGKILLed or power-cut GCS leaves behind. Everything that
    #: decoded before that point is returned.
    truncated: bool = False

    def series(self, drone_id: int, channel: str) -> tuple[list[int], list[float]]:
        """``(t_ms, values)`` for one drone and channel, values in the channel's unit.

        ``None`` entries mark ticks recorded before the drone joined the log.
        """
        spec = next(c for c in self.header["channels"] if c["key"] == channel)
        ts: list[int] = []
        vals: list[float] = []
        for tick in self.ticks:
            if drone_id not in tick["ids"]:
                continue
            raw = tick["ch"][channel][tick["ids"].index(drone_id)]
            ts.append(tick["t_ms"])
            vals.append(raw / spec["scale"] if raw != spec.get("missing", None) else float("nan"))
        return ts, vals


_READ_SLICE = 4096


def _decompressed_lines(path: str) -> Iterator[tuple[bytes, bool]]:
    """Yield ``(line, stream_ok)``; a final ``(b"", False)`` marks a truncated or damaged stream."""
    dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
    pending = b""
    with open(path, "rb") as f:
        # Small slices: a damaged spot costs only the slice it is in, not everything
        # decoded since the last read.
        while chunk := f.read(_READ_SLICE):
            try:
                data = dec.decompress(chunk)
            except zlib.error:
                yield b"", False
                return
            pending += data
            *lines, pending = pending.split(b"\n")
            for line in lines:
                yield line, True
    if not dec.eof or pending:
        yield b"", False


def iter_blackbox(path: str) -> Iterator[dict]:
    """Decoded records in file order: ``header``, ``tick`` (``key`` lines come
    out as ticks with ``key: True``), ``event``. Raises nothing for a damaged
    tail; a final ``{"type": "truncated"}`` record marks it.

    Tick records carry ``t_ms``, ``ids`` and ``ch`` - absolute quantised
    integers per channel, aligned with ``ids`` - and ``modes`` / ``fixes``
    dictionaries.
    """
    ids: list[int] = []
    prev: dict[str, list[int]] = {}
    modes: list[str] = []
    fixes: list[str] = []
    for line, ok in _decompressed_lines(path):
        if not ok:
            yield {"type": "truncated"}
            return
        try:
            rec = json.loads(line)
        except ValueError:
            yield {"type": "truncated"}
            return
        kind = rec.get("type")
        if kind == "key":
            ids, modes, fixes = rec["ids"], rec["modes"], rec["fixes"]
            prev = {k: list(v) for k, v in rec["ch"].items()}
            yield {"type": "tick", "key": True, "t_ms": rec["t"], "ids": ids, "ch": {k: list(v) for k, v in prev.items()},
                   "modes": modes, "fixes": fixes}
        elif kind == "tick":
            ch = {k: [p + d for p, d in zip(prev[k], diff)] for k, diff in rec["ch"].items()}
            prev = ch
            yield {"type": "tick", "key": False, "t_ms": rec["t"], "ids": ids, "ch": {k: list(v) for k, v in ch.items()},
                   "modes": modes, "fixes": fixes}
        elif kind == "header" or kind == "event":
            yield rec


def read_blackbox(path: str) -> BlackboxLog:
    """Load a whole log, tolerating a missing gzip trailer or a partial last line."""
    log: BlackboxLog | None = None
    for rec in iter_blackbox(path):
        kind = rec["type"]
        if kind == "header":
            log = BlackboxLog(header=rec)
        elif log is None:
            raise ValueError(f"{path} is not a {FORMAT_NAME} log: no header")
        elif kind == "tick":
            log.ticks.append(rec)
        elif kind == "event":
            log.events.append(rec)
        elif kind == "truncated":
            log.truncated = True
    if log is None:
        raise ValueError(f"{path} is not a {FORMAT_NAME} log: empty")
    return log
