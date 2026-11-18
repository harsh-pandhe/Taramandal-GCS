"""The on-drone show contract: a compact per-drone mission file, and the
broadcast pulse that starts every drone flying it at the same instant.

Streaming 10Hz offboard setpoints to each drone does not scale to a real swarm
radio — the downlink cost grows with fleet size, and a single lost stretch of
link trips PX4's offboard-loss failsafe mid-show. Instead, each drone is given
its whole trajectory ahead of time and flies it from its own storage, off its
own GNSS-disciplined clock. The GCS only has to say *when*, and that is one
broadcast frame no matter how many drones are listening.

Two wire formats live here, because both halves of the link parse them — the
GCS (``tara_gcs``) writes them, drone firmware (and ``tara_sim``, which stands
in for it) reads them — and neither half should own a copy the other can drift
from:

**Mission file** — one per drone, uploaded over MAVLink FTP during staging.
Little-endian throughout::

    header (40 bytes)
      4s   magic            b"TMSN"
      H    format version   MISSION_FORMAT_VERSION
      H    flags            bit 0: records carry LED colour
      H    drone_id         the physical drone this file was compiled for
      H    reserved         zero
      I    waypoint count
      I    duration_ms      time of the last waypoint
      16s  show_id          see compute_show_id()
      I    records_crc32    CRC-32 of the record block below
    records (16 bytes each)
      I    time_ms
      h    x_cm, y_cm, z_cm  NED, drone-local (origin = the drone's own pad)
      H    yaw_cdeg          0..35999
      B    r, g, b
      x    pad

Positions are centimetres in a signed 16-bit field, so ±327.67m — comfortably
past ``SafetyLimits.max_altitude_m`` and the geofence, and a show that exceeds
it is refused at compile time rather than wrapped around. A 10-minute show at
10 waypoints/s is ~96KB per drone.

**Show trigger** — one broadcast frame (``target_system = 0``)::

    B    command          ShowCommand
    16s  show_id          which show to start/cancel
    Q    start_utc_usec   UTC instant the show's t=0 maps to
    I    crc32            over the three fields above

A drone acts on START only if it holds a mission for that ``show_id``, which is
what stops a stale file from a previous rehearsal flying tonight's slot.

The emergency commands LAND, RTL and ESTOP (issue #57) ignore ``show_id``: they
must work even for a drone that holds no mission.

**Authenticated trigger** (issue #57). A CRC-32 only catches noise; anyone can
compute one, so a plain frame proves nothing about who sent it, and a forged
ESTOP would drop every drone in the air. An authenticated frame is the same
body followed by a replay counter and a keyed tag::

    <the 25-byte body above>
    Q    counter          strictly increasing per sender
    16s  tag              HMAC-SHA256(key, "tara-trigger-v1" + body + counter), first 16 bytes

A receiver holding a key accepts only authenticated frames whose tag verifies
and whose counter is greater than the last it accepted (so a recorded frame
cannot be replayed); a receiver without a key refuses every authenticated
frame and refuses LAND, RTL and ESTOP outright (fail closed). Plain CRC frames
remain valid for START and CANCEL on a keyless receiver, so the simulator
works with no setup. Key management (how the key reaches a drone) and radio-level
protection beyond this tag are not part of this module.
"""

from __future__ import annotations

import hashlib
import hmac
import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum

from .show import Show

MISSION_MAGIC = b"TMSN"

#: Bumped whenever the header or record layout changes incompatibly.
MISSION_FORMAT_VERSION = 1

#: Bytes of the SHA-256 show digest carried in headers and triggers. 128 bits
#: is far past collision-proof for "is this the same show", and halves the
#: space it costs in the trigger frame.
SHOW_ID_SIZE = 16

#: Header flag: records carry meaningful r/g/b values.
FLAG_HAS_LED = 0x0001

_HEADER_FORMAT = "<4sHHHHII16sI"
_RECORD_FORMAT = "<IhhhHBBBx"
HEADER_SIZE = struct.calcsize(_HEADER_FORMAT)
RECORD_SIZE = struct.calcsize(_RECORD_FORMAT)

_POS_LIMIT_CM = 32767
_MAX_TIME_MS = 0xFFFFFFFF


class ShowCommand(IntEnum):
    """What a broadcast show trigger asks every drone to do."""

    #: Begin flying the stored mission at ``start_utc_usec``.
    START = 1
    #: Abandon the mission (pending or in flight) and hold position.
    CANCEL = 2
    #: Emergency override (issue #57): every armed drone abandons its mission and
    #: descends straight down, whatever ``show_id`` the frame carries.
    LAND = 3
    #: Emergency override: every armed drone abandons its mission, flies back over
    #: its own pad at its current altitude and lands.
    RTL = 4
    #: Emergency override: every drone cuts its motors at once. There is no
    #: recovery from this in flight.
    ESTOP = 5


_TRIGGER_BODY_FORMAT = "<B16sQ"
_TRIGGER_BODY_SIZE = struct.calcsize(_TRIGGER_BODY_FORMAT)
TRIGGER_SIZE = _TRIGGER_BODY_SIZE + 4
#: An authenticated trigger: the body, an 8-byte replay counter and a 16-byte tag (see the module docstring).
TRIGGER_TAG_SIZE = 16
TRIGGER_AUTH_SIZE = _TRIGGER_BODY_SIZE + 8 + TRIGGER_TAG_SIZE
#: Shortest key accepted. HMAC-SHA256 takes any length, but a short key is a guessable one.
TRIGGER_KEY_MIN_BYTES = 16
_TRIGGER_TAG_CONTEXT = b"tara-trigger-v1"


@dataclass(frozen=True)
class MissionHeader:
    """Decoded mission-file header. See the module docstring for the layout."""

    version: int
    flags: int
    drone_id: int
    waypoint_count: int
    duration_ms: int
    show_id: bytes
    records_crc32: int

    @property
    def has_led(self) -> bool:
        return bool(self.flags & FLAG_HAS_LED)


def _cm(value_m: float, axis: str, drone_id: int) -> int:
    cm = round(value_m * 100.0)
    if not -_POS_LIMIT_CM <= cm <= _POS_LIMIT_CM:
        raise ValueError(
            f"drone {drone_id}: {axis}={value_m:.2f}m is outside the mission format's "
            f"±{_POS_LIMIT_CM / 100:.2f}m range"
        )
    return cm


def _encode_records(
    waypoints: list[dict], drone_id: int, origin: tuple[float, float]
) -> tuple[bytes, bool]:
    """Quantize and pack a drone's waypoints. Returns ``(records, has_led)``."""
    if not waypoints:
        raise ValueError(f"drone {drone_id}: cannot compile an empty waypoint list")

    ox, oy = origin
    has_led = any("r" in wp for wp in waypoints)
    out = bytearray()
    prev_ms = -1
    for wp in waypoints:
        time_ms = round(wp["time"] * 1000.0)
        if time_ms < 0 or time_ms > _MAX_TIME_MS:
            raise ValueError(f"drone {drone_id}: waypoint time {wp['time']}s is out of range")
        if time_ms < prev_ms:
            raise ValueError(
                f"drone {drone_id}: waypoints are not time-ordered "
                f"({time_ms}ms follows {prev_ms}ms)"
            )
        prev_ms = time_ms
        out += struct.pack(
            _RECORD_FORMAT,
            time_ms,
            _cm(wp["x"] - ox, "x", drone_id),
            _cm(wp["y"] - oy, "y", drone_id),
            _cm(wp["z"], "z", drone_id),
            round(wp.get("yaw", 0.0) * 100.0) % 36000,
            int(wp.get("r", 0)) & 0xFF,
            int(wp.get("g", 0)) & 0xFF,
            int(wp.get("b", 0)) & 0xFF,
        )
    return bytes(out), has_led


def compute_show_id(show) -> bytes:
    """A stable identity for a whole show, independent of which drone flies what.

    Digests every slot's waypoints in the shared fleet frame, quantized exactly
    as :func:`compile_mission` quantizes them — so two shows that compile to
    identical missions share an id, and float noise below a centimetre does not
    make an unchanged show look new.

    Args:
        show: A :class:`~tara_core.show.Show`, or either dict shape
            :meth:`Show.from_any` accepts.

    Returns:
        ``SHOW_ID_SIZE`` bytes.
    """
    show = Show.from_any(show)
    digest = hashlib.sha256()
    digest.update(MISSION_MAGIC + struct.pack("<H", MISSION_FORMAT_VERSION))
    for slot, wps in sorted(show.drones.items()):
        records, _ = _encode_records(wps, slot, (0.0, 0.0))
        digest.update(struct.pack("<HI", slot, len(wps)))
        digest.update(records)
    return digest.digest()[:SHOW_ID_SIZE]


def compile_mission(
    waypoints: list[dict],
    *,
    drone_id: int,
    show_id: bytes,
    origin: tuple[float, float] = (0.0, 0.0),
) -> bytes:
    """Compile one drone's waypoints into the binary mission file it will fly.

    Args:
        waypoints: Time-ordered waypoints in the shared fleet frame
            (``time``/``x``/``y``/``z``, optional ``yaw`` and ``r``/``g``/``b``).
        drone_id: The physical drone the file is for. Firmware can refuse a file
            addressed to someone else.
        show_id: From :func:`compute_show_id` over the whole show.
        origin: ``(x, y)`` of the drone's own launch pad in the fleet frame.
            Subtracted from every waypoint so the file is in the drone's own
            local NED frame, the one its autopilot actually flies in. Z has no
            offset — pads are flat.

    Raises:
        ValueError: On an empty or unordered waypoint list, or a position
            outside the format's range.
    """
    if len(show_id) != SHOW_ID_SIZE:
        raise ValueError(f"show_id must be {SHOW_ID_SIZE} bytes, got {len(show_id)}")
    records, has_led = _encode_records(waypoints, drone_id, origin)
    header = struct.pack(
        _HEADER_FORMAT,
        MISSION_MAGIC,
        MISSION_FORMAT_VERSION,
        FLAG_HAS_LED if has_led else 0,
        drone_id,
        0,
        len(waypoints),
        round(waypoints[-1]["time"] * 1000.0),
        show_id,
        zlib.crc32(records),
    )
    return header + records


def decode_mission_header(blob: bytes) -> MissionHeader:
    """Parse and sanity-check just the header of a mission file.

    Raises:
        ValueError: If the blob is too short, not a mission file, or a version
            this code does not understand.
    """
    if len(blob) < HEADER_SIZE:
        raise ValueError(f"mission file is {len(blob)} bytes, shorter than its {HEADER_SIZE}-byte header")
    magic, version, flags, drone_id, _, count, duration_ms, show_id, crc = struct.unpack_from(
        _HEADER_FORMAT, blob
    )
    if magic != MISSION_MAGIC:
        raise ValueError(f"not a mission file (magic {magic!r})")
    if version != MISSION_FORMAT_VERSION:
        raise ValueError(f"unsupported mission format version {version}")
    return MissionHeader(version, flags, drone_id, count, duration_ms, show_id, crc)


def decode_mission(blob: bytes) -> tuple[MissionHeader, list[dict]]:
    """Parse a mission file back into its header and drone-local waypoints.

    This is the read side firmware runs before accepting a file: a truncated
    upload or a flipped bit fails here rather than flying.

    Returns:
        ``(header, waypoints)``. Waypoints carry ``time``/``x``/``y``/``z``/
        ``yaw`` (seconds, metres NED, degrees), plus ``r``/``g``/``b`` when the
        header says the show has light data.

    Raises:
        ValueError: On any structural or checksum failure.
    """
    header = decode_mission_header(blob)
    records = blob[HEADER_SIZE:]
    expected = header.waypoint_count * RECORD_SIZE
    if len(records) != expected:
        raise ValueError(
            f"mission file carries {len(records)} record bytes, header promises {expected}"
        )
    if zlib.crc32(records) != header.records_crc32:
        raise ValueError("mission records fail their CRC-32 — file is corrupt")

    waypoints = []
    for time_ms, x, y, z, yaw, r, g, b in struct.iter_unpack(_RECORD_FORMAT, records):
        wp = {"time": time_ms / 1000.0, "x": x / 100.0, "y": y / 100.0, "z": z / 100.0, "yaw": yaw / 100.0}
        if header.has_led:
            wp.update(r=r, g=g, b=b)
        waypoints.append(wp)
    return header, waypoints


def mission_crc32(blob: bytes) -> int:
    """CRC-32 of a whole mission file, as reported in upload/verification status."""
    return zlib.crc32(blob)


def _check_trigger_key(key) -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) < TRIGGER_KEY_MIN_BYTES:
        raise ValueError(f"a trigger key must be at least {TRIGGER_KEY_MIN_BYTES} bytes")
    return bytes(key)


def _trigger_tag(key: bytes, body: bytes, counter: int) -> bytes:
    mac = hmac.new(key, _TRIGGER_TAG_CONTEXT + body + struct.pack("<Q", counter), hashlib.sha256)
    return mac.digest()[:TRIGGER_TAG_SIZE]


def encode_show_trigger(
    command: ShowCommand | int,
    show_id: bytes,
    start_utc_usec: int = 0,
    *,
    key: bytes | None = None,
    counter: int | None = None,
) -> bytes:
    """Pack a broadcast show trigger. See the module docstring for the layout.

    With no ``key`` this is the plain CRC-32 frame. With a ``key`` it is the authenticated frame, and
    ``counter`` (a positive integer, greater than every counter this sender used before) is required.
    """
    if len(show_id) != SHOW_ID_SIZE:
        raise ValueError(f"show_id must be {SHOW_ID_SIZE} bytes, got {len(show_id)}")
    body = struct.pack(_TRIGGER_BODY_FORMAT, int(ShowCommand(command)), show_id, start_utc_usec)
    if key is None:
        if counter is not None:
            raise ValueError("a counter only goes with a key")
        return body + struct.pack("<I", zlib.crc32(body))
    key = _check_trigger_key(key)
    if not isinstance(counter, int) or isinstance(counter, bool) or not 0 < counter < 1 << 64:
        raise ValueError("an authenticated trigger needs a counter in 1..2**64-1")
    return body + struct.pack("<Q", counter) + _trigger_tag(key, body, counter)


def decode_show_trigger(payload: bytes, key: bytes | None = None) -> dict:
    """Unpack a broadcast show trigger.

    Args:
        payload: A plain frame (``TRIGGER_SIZE`` bytes) or an authenticated one (``TRIGGER_AUTH_SIZE`` bytes).
        key: The receiver's trigger key, if it has one. Needed to verify an authenticated frame.

    Returns:
        ``{"command", "show_id", "start_utc_usec", "valid", "authenticated", "counter"}``. ``valid`` is False on a
        checksum or tag mismatch, an unknown command, or an authenticated frame that this receiver has no key to
        check; as with ``tara_gcs.broadcast_manager.decode_broadcast_frame``, the receiver decides what to do about
        it (on a drone: ignore the frame). ``authenticated`` is True only for an authenticated frame whose tag
        verified under ``key``. ``counter`` is the frame's replay counter (None on a plain frame); the receiver
        must check it against the last one it accepted, which this function cannot know.

    Raises:
        ValueError: If the payload is the wrong size to be a trigger at all.
    """
    if len(payload) not in (TRIGGER_SIZE, TRIGGER_AUTH_SIZE):
        raise ValueError(f"expected a {TRIGGER_SIZE}- or {TRIGGER_AUTH_SIZE}-byte trigger, got {len(payload)}")
    body = payload[:_TRIGGER_BODY_SIZE]
    command, show_id, start_utc_usec = struct.unpack(_TRIGGER_BODY_FORMAT, body)
    known = command in ShowCommand._value2member_map_
    if len(payload) == TRIGGER_SIZE:
        (crc,) = struct.unpack_from("<I", payload, _TRIGGER_BODY_SIZE)
        valid, authenticated, counter = crc == zlib.crc32(body) and known, False, None
    else:
        (counter,) = struct.unpack_from("<Q", payload, _TRIGGER_BODY_SIZE)
        tag = payload[_TRIGGER_BODY_SIZE + 8 :]
        authenticated = False
        if key is not None:
            expected = _trigger_tag(_check_trigger_key(key), body, counter)
            authenticated = hmac.compare_digest(tag, expected)
        valid = authenticated and known
    return {
        "command": command,
        "show_id": show_id,
        "start_utc_usec": start_utc_usec,
        "valid": valid,
        "authenticated": authenticated and valid,
        "counter": counter,
    }
