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
"""

from __future__ import annotations

import hashlib
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


_TRIGGER_BODY_FORMAT = "<B16sQ"
_TRIGGER_BODY_SIZE = struct.calcsize(_TRIGGER_BODY_FORMAT)
TRIGGER_SIZE = _TRIGGER_BODY_SIZE + 4


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


def encode_show_trigger(command: ShowCommand | int, show_id: bytes, start_utc_usec: int = 0) -> bytes:
    """Pack a broadcast show trigger. See the module docstring for the layout."""
    if len(show_id) != SHOW_ID_SIZE:
        raise ValueError(f"show_id must be {SHOW_ID_SIZE} bytes, got {len(show_id)}")
    body = struct.pack(_TRIGGER_BODY_FORMAT, int(ShowCommand(command)), show_id, start_utc_usec)
    return body + struct.pack("<I", zlib.crc32(body))


def decode_show_trigger(payload: bytes) -> dict:
    """Unpack a broadcast show trigger.

    Returns:
        ``{"command", "show_id", "start_utc_usec", "valid"}``. ``valid`` is
        False on a checksum mismatch or an unknown command; as with
        ``tara_gcs.broadcast_manager.decode_broadcast_frame``, the receiver
        decides what to do about it (on a drone: ignore the frame).

    Raises:
        ValueError: If the payload is the wrong size to be a trigger at all.
    """
    if len(payload) != TRIGGER_SIZE:
        raise ValueError(f"expected {TRIGGER_SIZE}-byte trigger, got {len(payload)}")
    body = payload[:_TRIGGER_BODY_SIZE]
    command, show_id, start_utc_usec = struct.unpack(_TRIGGER_BODY_FORMAT, body)
    (crc,) = struct.unpack_from("<I", payload, _TRIGGER_BODY_SIZE)
    valid = crc == zlib.crc32(body) and command in ShowCommand._value2member_map_
    return {
        "command": command,
        "show_id": show_id,
        "start_utc_usec": start_utc_usec,
        "valid": valid,
    }
