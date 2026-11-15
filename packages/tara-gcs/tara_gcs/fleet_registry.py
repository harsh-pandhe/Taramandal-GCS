"""Fleet registry: who is in the fleet, by hardware identity, and who has gone quiet (issue #58).

The GCS has always assumed a fixed list of ports (``14540 + i``). A real fleet on a Wi-Fi or radio
subnet has no such list: drones announce themselves. This module is the part that decides what an
announcement means, with no sockets and no clock of its own, so every rule here is tested exactly:

* ``encode_announcement`` / ``decode_announcement``: the 30-byte announcement frame a drone sends
  (about 2 Hz). It carries the drone's 64-bit hardware id and its clock, and an 8-byte HMAC tag
  under a pre-shared key. **An announcement that does not verify changes nothing**: without the tag
  anyone on the network could claim a slot, take over another drone's identity, or keep a dead drone
  looking alive (the same reasoning as the broadcast downlink, issue #50).
* ``FleetRegistry``: ``hardware_uuid -> drone_id``. A new id gets the lowest free slot; an id seen
  before gets its **original** slot back, however it reconnects (so a re-powered drone returns to
  its pad). A drone that stops announcing is marked ``STALE`` within ``stale_after_s``
  (default 1.5 s: three missed heartbeats at 2 Hz) and is never counted online again until a
  verified announcement arrives. Nothing here infers readiness: a drone is ``ONLINE`` only while
  its announcements keep arriving.

What this does not do: it does not connect to a discovered drone, change ``DroneManager``, or make
any flight decision. It is the registry and the watchdog; the opt-in listener that feeds it is
``fleet_discovery``.

Time. Frames are checked against the drone's own clock (it needs one independent of this link,
such as GPS time, as for ``BroadcastReceiver``): a frame more than ``max_skew_s`` from the GCS
wall clock is dropped, and a frame whose timestamp is not above the last accepted one for that
drone is dropped as a replay, which also means a drone that reboots (its counter restarts but its
clock does not) is still accepted. Staleness uses the monotonic clock, so stepping the wall clock
cannot make the whole fleet look dead or alive.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import struct
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from tara_core import MAX_FLEET_SIZE

logger = logging.getLogger(__name__)

#: Wire layout, little-endian, no padding: magic(4) version(1) flags(1) hardware_uuid(8)
#: timestamp_usec(8) then the 8-byte tag over everything before it.
_BODY_FORMAT = "<4sBBQQ"
MAGIC = b"TANN"
VERSION = 1
AUTH_TAG_SIZE = 8
BODY_SIZE = struct.calcsize(_BODY_FORMAT)
FRAME_SIZE = BODY_SIZE + AUTH_TAG_SIZE

#: Prefixed to every MAC input so a tag made for an announcement can never validate another message
#: type that happens to reuse the key.
_AUTH_DOMAIN = b"TARA-ANNOUNCE\x01"
KEY_MIN_BYTES = 16

UUID_MAX = 2**64 - 1

#: Three missed heartbeats at 2 Hz (the issue's wording), and how often to look.
DEFAULT_STALE_AFTER_S = 1.5
#: How far a drone's clock may be from the GCS wall clock. Bounds how long a captured frame can be
#: replayed (and only frames the GCS never saw can be: a frame at or below the last accepted
#: timestamp is dropped).
DEFAULT_MAX_SKEW_S = 0.5


class Status(str, Enum):
    """Why an announcement was accepted or dropped."""

    OK = "ok"
    MALFORMED = "malformed"
    BAD_TAG = "bad_tag"
    CLOCK_SKEW = "clock_skew"
    REPLAYED = "replayed"
    FLEET_FULL = "fleet_full"


class State(str, Enum):
    ONLINE = "ONLINE"
    STALE = "STALE"


def _check_key(key: bytes) -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) < KEY_MIN_BYTES:
        raise ValueError(f"the discovery key must be at least {KEY_MIN_BYTES} bytes")
    return bytes(key)


def _tag(key: bytes, body: bytes) -> bytes:
    return hmac.new(key, _AUTH_DOMAIN + body, hashlib.sha256).digest()[:AUTH_TAG_SIZE]


def encode_announcement(key: bytes, hardware_uuid: int, timestamp_usec: int | None = None) -> bytes:
    """One announcement frame, as a drone sends it. ``timestamp_usec`` defaults to the wall clock."""
    key = _check_key(key)
    if not isinstance(hardware_uuid, int) or isinstance(hardware_uuid, bool) or not 0 < hardware_uuid <= UUID_MAX:
        raise ValueError("hardware_uuid must be an integer from 1 to 2**64 - 1")
    ts = int(time.time() * 1_000_000) if timestamp_usec is None else int(timestamp_usec)
    if not 0 <= ts <= UUID_MAX:
        raise ValueError("timestamp_usec does not fit in 64 bits")
    body = struct.pack(_BODY_FORMAT, MAGIC, VERSION, 0, hardware_uuid, ts)
    return body + _tag(key, body)


def decode_announcement(payload: bytes, key: bytes) -> dict:
    """Check the shape and the tag of one frame. ``status`` is OK, MALFORMED or BAD_TAG.

    Clock and replay checks need state, so they are the registry's. The tag is checked before any
    field is trusted for anything.
    """
    key = _check_key(key)
    if not isinstance(payload, (bytes, bytearray)) or len(payload) != FRAME_SIZE:
        return {"status": Status.MALFORMED}
    body, tag = bytes(payload[:BODY_SIZE]), bytes(payload[BODY_SIZE:])
    magic, version, _flags, uuid, ts = struct.unpack(_BODY_FORMAT, body)
    if magic != MAGIC or version != VERSION or uuid == 0:
        return {"status": Status.MALFORMED}
    if not hmac.compare_digest(tag, _tag(key, body)):
        return {"status": Status.BAD_TAG}
    return {"status": Status.OK, "hardware_uuid": uuid, "timestamp_usec": ts}


@dataclass
class Entry:
    hardware_uuid: int
    drone_id: int
    state: State = State.STALE
    #: Monotonic time of the last verified announcement, None until one arrives in this run.
    last_seen_mono: float | None = None
    last_seen_wall: float | None = None
    last_timestamp_usec: int = 0
    first_seen_wall: float | None = None
    stale_count: int = 0

    @property
    def pad_index(self) -> int:
        """The launch pad: the pad grid is indexed by drone id (``tara_core.launch_grid``)."""
        return self.drone_id


@dataclass
class Observation:
    status: Status
    entry: Entry | None = None
    joined: bool = False  # first time this hardware id has ever been seen
    recovered: bool = False  # was STALE (or only remembered), is ONLINE again


@dataclass
class Transition:
    """A change a caller may want to tell someone about."""

    kind: str  # "joined" | "recovered" | "stale"
    hardware_uuid: int
    drone_id: int
    at_wall: float


@dataclass
class FleetRegistry:
    """See the module docstring. Not thread-safe: use it from one event loop."""

    key: bytes
    max_fleet: int = MAX_FLEET_SIZE
    stale_after_s: float = DEFAULT_STALE_AFTER_S
    max_skew_s: float = DEFAULT_MAX_SKEW_S
    #: Where the id -> slot bindings are remembered across a GCS restart. None keeps them in memory only.
    store_path: str | os.PathLike | None = None
    wall_clock: object = time.time
    mono_clock: object = time.monotonic
    entries: dict[int, Entry] = field(default_factory=dict, init=False)
    rejected: dict[str, int] = field(default_factory=lambda: {s.value: 0 for s in Status if s is not Status.OK}, init=False)
    accepted: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.key = _check_key(self.key)
        if not 1 <= self.max_fleet <= MAX_FLEET_SIZE:
            raise ValueError(f"max_fleet must be 1..{MAX_FLEET_SIZE}")
        if self.stale_after_s <= 0 or self.max_skew_s <= 0:
            raise ValueError("stale_after_s and max_skew_s must be positive")
        if self.store_path is not None:
            self._load()

    # ------------------------------------------------------------ announcements

    def observe(self, payload: bytes) -> Observation:
        """Handle one received frame. Anything that is not accepted leaves the registry untouched."""
        frame = decode_announcement(payload, self.key)
        if frame["status"] is not Status.OK:
            return self._reject(frame["status"])
        uuid, ts = frame["hardware_uuid"], frame["timestamp_usec"]
        now_wall = self.wall_clock()
        if abs(now_wall * 1_000_000 - ts) > self.max_skew_s * 1_000_000:
            return self._reject(Status.CLOCK_SKEW)
        entry = self.entries.get(uuid)
        if entry is not None and ts <= entry.last_timestamp_usec:
            return self._reject(Status.REPLAYED)

        joined = entry is None
        if joined:
            slot = self._free_slot()
            if slot is None:
                return self._reject(Status.FLEET_FULL)
            entry = Entry(hardware_uuid=uuid, drone_id=slot, first_seen_wall=now_wall)
            self.entries[uuid] = entry
            self._save()
        recovered = not joined and entry.state is State.STALE
        entry.state = State.ONLINE
        entry.last_seen_mono = self.mono_clock()
        entry.last_seen_wall = now_wall
        entry.last_timestamp_usec = ts
        self.accepted += 1
        if joined:
            logger.info("Fleet registry: drone %016x joined as drone %d", uuid, entry.drone_id)
        elif recovered:
            logger.info("Fleet registry: drone %d (%016x) is back", entry.drone_id, uuid)
        return Observation(Status.OK, entry, joined=joined, recovered=recovered)

    def _reject(self, status: Status) -> Observation:
        self.rejected[status.value] += 1
        return Observation(status)

    def _free_slot(self) -> int | None:
        taken = {e.drone_id for e in self.entries.values()}
        return next((i for i in range(self.max_fleet) if i not in taken), None)

    # ---------------------------------------------------------------- watchdog

    def sweep(self) -> list[Transition]:
        """Mark every drone that has gone quiet STALE. Call it often; it only compares timestamps."""
        now = self.mono_clock()
        out: list[Transition] = []
        for entry in self.entries.values():
            if entry.state is State.ONLINE and now - entry.last_seen_mono > self.stale_after_s:
                entry.state = State.STALE
                entry.stale_count += 1
                out.append(Transition("stale", entry.hardware_uuid, entry.drone_id, self.wall_clock()))
                logger.warning(
                    "Fleet registry: drone %d (%016x) is STALE, no announcement for %.1f s",
                    entry.drone_id, entry.hardware_uuid, now - entry.last_seen_mono,
                )
        return out

    # ----------------------------------------------------------------- queries

    def release(self, hardware_uuid: int) -> bool:
        """Forget a drone and free its slot (a drone removed from the fleet for good)."""
        if self.entries.pop(hardware_uuid, None) is None:
            return False
        self._save()
        return True

    def online_ids(self) -> list[int]:
        return sorted(e.drone_id for e in self.entries.values() if e.state is State.ONLINE)

    def snapshot(self) -> dict:
        now = self.mono_clock()
        drones = [
            {
                "drone_id": e.drone_id,
                "pad_index": e.pad_index,
                "hardware_uuid": f"{e.hardware_uuid:016x}",
                "state": e.state.value,
                "age_s": None if e.last_seen_mono is None else round(now - e.last_seen_mono, 2),
                "last_seen": e.last_seen_wall,
                "stale_count": e.stale_count,
            }
            for e in sorted(self.entries.values(), key=lambda e: e.drone_id)
        ]
        return {
            "stale_after_s": self.stale_after_s,
            "online": sum(1 for d in drones if d["state"] == "ONLINE"),
            "stale": sum(1 for d in drones if d["state"] == "STALE"),
            "capacity": self.max_fleet,
            "accepted": self.accepted,
            "rejected": dict(self.rejected),
            "drones": drones,
        }

    # ------------------------------------------------------------- persistence

    def _save(self) -> None:
        if self.store_path is None:
            return
        path = Path(self.store_path)
        doc = {"format": "tara-fleet-registry", "version": 1,
               "slots": {f"{u:016x}": e.drone_id for u, e in sorted(self.entries.items())}}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError:
            # Losing the file only costs a returning drone its old slot after a GCS restart; the
            # fleet itself keeps working, so say so loudly and carry on.
            logger.exception("Fleet registry: could not save %s; slots will not survive a restart", path)

    def _load(self) -> None:
        path = Path(self.store_path)
        if not path.exists():
            return
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            slots = doc["slots"]
            loaded: dict[int, Entry] = {}
            used: set[int] = set()
            for text, drone_id in slots.items():
                uuid = int(text, 16)
                ok = (isinstance(drone_id, int) and not isinstance(drone_id, bool) and 0 <= drone_id < self.max_fleet
                      and 0 < uuid <= UUID_MAX and drone_id not in used)
                if not ok:
                    raise ValueError(f"bad entry {text!r}: {drone_id!r}")
                used.add(drone_id)
                loaded[uuid] = Entry(hardware_uuid=uuid, drone_id=drone_id)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            aside = path.with_name(path.name + ".corrupt")
            logger.error("Fleet registry: %s is unreadable (%s); starting empty, kept it as %s", path, exc, aside)
            try:
                os.replace(path, aside)
            except OSError:
                pass
            return
        # Remembered, not heard: STALE until a verified announcement says otherwise.
        self.entries = loaded
