"""Read-side of the black-box flight recorder, for the Replay view (issue #65).

:mod:`tara_gcs.blackbox` writes a differential, quantised ``.jsonl.gz`` log.
This module turns a finished (or still-growing, or truncated) session file
into what a browser can scrub through:

* :func:`list_sessions` - the files in the recorder directory, newest first,
  with the header's start time and context. Only the first line of each file
  is decompressed, so listing a directory of 100 MB files is cheap.
* :class:`ReplaySession` - a whole file decoded once into compact numpy arrays
  (one ``int32`` array per tick, not Python lists), so a 15 minute 100 drone
  log is tens of megabytes in memory and a time-window query is a binary
  search plus a slice.
* :class:`ReplayStore` - a one-entry cache keyed on ``(path, mtime, size)``,
  so scrubbing does not re-decompress the file on every request but a
  session that is still being recorded is re-read when it has grown.

Session ids are file names (``blackbox_<UTC start>_<pid>[_n]``), matched
against a strict pattern and then looked up *inside* the recorder directory,
so no request can name a path outside it.

Everything here is blocking (decompression, numpy): callers on the GCS event
loop must run it in a worker thread so a replay request never delays the
10 Hz safety loops.
"""

from __future__ import annotations

import json
import os
import hashlib
import re
import secrets
import threading
import zlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np

from tara_gcs.blackbox import FLAG_BITS, FORMAT_NAME, _decompressed_lines, iter_blackbox

SESSION_SUFFIX = ".jsonl.gz"
_SESSION_ID = re.compile(r"^blackbox_[0-9A-Za-z_\-]{1,120}$")

#: Events that belong on the timeline as clickable markers (issue #65: "safety
#: events pinned as clickable markers"). Commands and lifecycle transitions are
#: in the event list but are not markers by default.
SAFETY_EVENT_KINDS = frozenset(
    {
        "proximity_breach",
        "proximity_clear",
        "geofence_hard_breach",
        "geofence_hard_clear",
        "geofence_soft_breach",
        "geofence_soft_clear",
    }
)

#: Channels returned as plain numbers; the others are decoded specially.
_NUMERIC_KEYS = ("x", "y", "z", "h", "b", "v", "n", "l", "r")
#: Output names, in the same order as ``_NUMERIC_KEYS``. Units: metres, degrees,
#: percent, volts, count, milliseconds, fraction 0..1.
OUTPUT_NAMES = {
    "x": "x_m", "y": "y_m", "z": "z_m", "h": "heading_deg", "b": "battery_percent",
    "v": "battery_v", "n": "satellites", "l": "latency_ms", "r": "rf_drop_rate",
}

#: Event fields that name a person or where they connected from (see ``main.py`` and
#: ``preflight.py`` audit calls). Operator ids become a stable pseudonym, addresses are hidden.
PSEUDONYM_KEYS = frozenset({"operator", "requested_by"})
HIDDEN_KEYS = frozenset({"origin_ip"})
HIDDEN = "[hidden]"
#: Actors that are the GCS itself, not a person; kept as they are so a failsafe still reads
#: "system".
_NON_PERSON = frozenset({"system"})
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
#: Per process, so a pseudonym is stable across requests but cannot be reversed or compared
#: between GCS runs.
_SALT = secrets.token_bytes(16)

DEFAULT_MAX_FRAMES = 1500
HARD_MAX_FRAMES = 5000
#: Cap on frames x drones in one reply (~85 bytes of JSON each): a whole-session
#: overview of 100 drones is thinned to this rather than returned as 13 MB.
DEFAULT_MAX_SAMPLES = 50_000
HARD_MAX_SAMPLES = 200_000
DEFAULT_MAX_EVENTS = 5000
HARD_MAX_EVENTS = 20000


class ReplayError(Exception):
    """A request the reader cannot satisfy (unknown session, unreadable file)."""


class SessionNotFound(ReplayError):
    pass


def pseudonym(operator_id: str) -> str:
    """A stable, non-reversible stand-in for an operator id (``operator-3fa9c1``)."""
    return "operator-" + hashlib.sha256(_SALT + operator_id.encode()).hexdigest()[:6]


def redact_identities(value: Any) -> Any:
    """A copy of an event (or any JSON value) with operator ids pseudonymised and IP addresses
    hidden: the identity fields by name, and any IPv4 address inside a string value.

    Free text (a ``reason`` an operator typed, an exception message) is only scrubbed of IPv4
    addresses; a name typed into free text is not recognised.
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in HIDDEN_KEYS and v is not None:
                out[k] = HIDDEN
            elif k in PSEUDONYM_KEYS and isinstance(v, str) and v not in _NON_PERSON:
                out[k] = pseudonym(v)
            else:
                out[k] = redact_identities(v)
        return out
    if isinstance(value, list):
        return [redact_identities(v) for v in value]
    if isinstance(value, str):
        return _IPV4.sub(HIDDEN, value)
    return value


def valid_session_id(session_id: str) -> bool:
    return bool(_SESSION_ID.match(session_id))


def session_path(directory: str, session_id: str) -> str:
    """The file for ``session_id`` inside ``directory``; :class:`SessionNotFound` otherwise."""
    if not valid_session_id(session_id):
        raise SessionNotFound(session_id)
    path = os.path.join(directory, session_id + SESSION_SUFFIX)
    # The pattern already excludes separators and dots; this also refuses a symlink
    # that would lead out of the recorder directory.
    real_dir = os.path.realpath(directory)
    if os.path.dirname(os.path.realpath(path)) != real_dir or not os.path.isfile(path):
        raise SessionNotFound(session_id)
    return path


def read_header(path: str) -> dict | None:
    """The header line of a session file, or ``None`` if it is not a readable log."""
    try:
        for line, ok in _decompressed_lines(path):
            if not ok:
                return None
            rec = json.loads(line)
            return rec if rec.get("type") == "header" and rec.get("format") == FORMAT_NAME else None
    except (OSError, ValueError, zlib.error):
        return None
    return None


def list_sessions(directory: str, *, live_path: str | None = None) -> list[dict]:
    """Session files in ``directory``, newest first. Unreadable files are skipped."""
    out: list[dict] = []
    try:
        names = os.listdir(directory)
    except OSError:
        return out
    for name in names:
        if not name.endswith(SESSION_SUFFIX):
            continue
        session_id = name[: -len(SESSION_SUFFIX)]
        if not valid_session_id(session_id):
            continue
        path = os.path.join(directory, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        header = read_header(path)
        if header is None:
            continue
        out.append(
            {
                "id": session_id,
                "bytes": st.st_size,
                "modified": st.st_mtime,
                "started": header.get("started"),
                "context": redact_identities(header.get("context", {})),
                "live": live_path is not None and os.path.abspath(path) == os.path.abspath(live_path),
            }
        )
    out.sort(key=lambda s: (s["modified"], s["id"]), reverse=True)
    return out


@dataclass
class _Tick:
    t_ms: int
    ids: list[int]
    values: np.ndarray  # (channels, drones) int32 in header channel order
    modes: list[str]
    fixes: list[str]


@dataclass
class ReplaySession:
    """A decoded log: ticks as compact arrays plus the event list."""

    session_id: str
    header: dict
    keys: list[str]
    scales: dict[str, float]
    missing: dict[str, int]
    t_ms: np.ndarray
    ticks: list[_Tick]
    events: list[dict]
    truncated: bool
    size_bytes: int = 0
    _all_ids: list[int] = field(default_factory=list)

    @classmethod
    def load(cls, path: str, session_id: str) -> "ReplaySession":
        header: dict | None = None
        keys: list[str] = []
        ticks: list[_Tick] = []
        events: list[dict] = []
        truncated = False
        try:
            for rec in iter_blackbox(path):
                kind = rec["type"]
                if kind == "header":
                    header = rec
                    keys = [c["key"] for c in rec["channels"]]
                elif header is None:
                    raise ReplayError(f"{session_id} is not a {FORMAT_NAME} log: no header")
                elif kind == "tick":
                    ids = rec["ids"]
                    values = np.array([rec["ch"][k] for k in keys], dtype=np.int32).reshape(len(keys), len(ids))
                    ticks.append(_Tick(rec["t_ms"], ids, values, rec["modes"], rec["fixes"]))
                elif kind == "event":
                    events.append(rec)
                elif kind == "truncated":
                    truncated = True
        except OSError as e:
            raise ReplayError(f"{session_id}: {e}") from e
        if header is None:
            raise ReplayError(f"{session_id} is not a {FORMAT_NAME} log: empty")
        channels = header["channels"]
        ids_seen: set[int] = set()
        for t in ticks:
            ids_seen.update(t.ids)
        return cls(
            session_id=session_id,
            header=header,
            keys=keys,
            scales={c["key"]: c["scale"] for c in channels},
            missing={c["key"]: c["missing"] for c in channels if "missing" in c},
            t_ms=np.array([t.t_ms for t in ticks], dtype=np.int64),
            ticks=ticks,
            events=events,
            truncated=truncated,
            size_bytes=os.path.getsize(path),
            _all_ids=sorted(ids_seen),
        )

    # ------------------------------------------------------------- summary

    @property
    def duration_ms(self) -> int:
        last_event = max((int(e.get("t", 0)) for e in self.events), default=0)
        last_tick = int(self.t_ms[-1]) if len(self.t_ms) else 0
        return max(last_tick, last_event)

    def summary(self, *, reveal_identities: bool = False) -> dict:
        kinds: dict[str, int] = {}
        for e in self.events:
            kinds[e.get("kind", "?")] = kinds.get(e.get("kind", "?"), 0) + 1
        return {
            "id": self.session_id,
            "header": self.header if reveal_identities else redact_identities(self.header),
            "identities_hidden": not reveal_identities,
            "duration_ms": self.duration_ms,
            "first_tick_ms": int(self.t_ms[0]) if len(self.t_ms) else None,
            "ticks": len(self.ticks),
            "drone_ids": self._all_ids,
            "events": len(self.events),
            "event_kinds": kinds,
            "flag_bits": FLAG_BITS,
            "truncated": self.truncated,
            "bytes": self.size_bytes,
        }

    # -------------------------------------------------------------- events

    def event_list(
        self, kinds: Iterable[str] | None = None, *, limit: int = DEFAULT_MAX_EVENTS, reveal_identities: bool = False
    ) -> dict:
        wanted = set(kinds) if kinds is not None else None
        limit = max(1, min(int(limit), HARD_MAX_EVENTS))
        rows = [e for e in self.events if wanted is None or e.get("kind") in wanted]
        total = len(rows)
        rows = rows[:limit]
        if not reveal_identities:
            rows = [redact_identities(e) for e in rows]
        return {"events": rows, "total": total, "limited": total > limit, "identities_hidden": not reveal_identities}

    # -------------------------------------------------------------- frames

    def frames(
        self,
        from_ms: int | None = None,
        to_ms: int | None = None,
        *,
        step_ms: int = 0,
        max_frames: int = DEFAULT_MAX_FRAMES,
        max_samples: int = DEFAULT_MAX_SAMPLES,
        drones: Sequence[int] | None = None,
    ) -> dict:
        """Ticks in ``[from_ms, to_ms]`` in real units, thinned to at most ``max_frames``.

        ``step_ms`` keeps at most one tick per step (``0`` = every tick). If the
        window still holds more than ``max_frames`` ticks, a uniform stride is
        applied and ``"stride"`` in the reply says so. ``max_samples`` additionally
        caps frames x drones, so a wide fleet gets fewer frames, not a huge reply.
        """
        max_frames = max(1, min(int(max_frames), HARD_MAX_FRAMES))
        max_samples = max(1, min(int(max_samples), HARD_MAX_SAMPLES))
        wanted = None if drones is None else set(int(d) for d in drones)
        width = len(self._all_ids) if wanted is None else len(wanted & set(self._all_ids))
        max_frames = max(1, min(max_frames, max_samples // max(width, 1)))
        n = len(self.ticks)
        lo = 0 if from_ms is None else int(np.searchsorted(self.t_ms, from_ms, side="left"))
        hi = n if to_ms is None else int(np.searchsorted(self.t_ms, to_ms, side="right"))
        idx = np.arange(lo, hi)
        if step_ms and step_ms > 0 and len(idx):
            # first tick of each step-sized bucket
            buckets = (self.t_ms[idx] - self.t_ms[idx[0]]) // int(step_ms)
            keep = np.concatenate(([True], buckets[1:] != buckets[:-1]))
            idx = idx[keep]
        stride = 1
        if len(idx) > max_frames:
            stride = -(-len(idx) // max_frames)
            idx = idx[::stride]
        frames = [self._frame(self.ticks[i], wanted) for i in idx]
        return {
            "t_ms_first": int(self.t_ms[idx[0]]) if len(idx) else None,
            "t_ms_last": int(self.t_ms[idx[-1]]) if len(idx) else None,
            "stride": stride,
            "count": len(frames),
            "frames": frames,
        }

    def _frame(self, tick: _Tick, wanted: set[int] | None) -> dict:
        if wanted is None:
            cols = np.arange(len(tick.ids))
        else:
            cols = np.array([j for j, d in enumerate(tick.ids) if d in wanted], dtype=np.intp)
        values = tick.values[:, cols]  # (channels, selected drones)
        row = {k: values[i] for i, k in enumerate(self.keys)}
        out: dict[str, Any] = {"t_ms": tick.t_ms, "ids": [tick.ids[j] for j in cols]}
        for k in _NUMERIC_KEYS:
            if k not in row:
                continue
            raw = row[k]
            scaled = np.round(raw / self.scales[k], 4).tolist()
            miss = self.missing.get(k)
            if miss is not None:
                scaled = [None if r == miss else v for r, v in zip(raw.tolist(), scaled)]
            out[OUTPUT_NAMES[k]] = scaled
        if "s" in row:
            out["flags"] = row["s"].tolist()
        if "m" in row:
            out["mode"] = [_name(tick.modes, i) for i in row["m"].tolist()]
        if "f" in row:
            out["gps_fix"] = [_name(tick.fixes, i) for i in row["f"].tolist()]
        return out


def _name(table: list[str], idx: int) -> str:
    return table[idx] if 0 <= idx < len(table) else "UNKNOWN"


class ReplayStore:
    """One decoded session at a time, re-read if the file changed.

    Keeping a single entry bounds memory to one session. Decoding is serialised
    so two simultaneous requests for the same cold file decode it once.
    """

    def __init__(self, directory: str):
        self.directory = directory
        self._lock = threading.Lock()
        self._key: tuple | None = None
        self._session: ReplaySession | None = None

    def get(self, session_id: str) -> ReplaySession:
        path = session_path(self.directory, session_id)
        try:
            st = os.stat(path)
        except OSError as e:
            raise SessionNotFound(session_id) from e
        key = (path, st.st_mtime_ns, st.st_size)
        with self._lock:
            if self._key != key or self._session is None:
                self._session = ReplaySession.load(path, session_id)
                self._key = key
            return self._session
