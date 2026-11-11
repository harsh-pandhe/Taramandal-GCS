"""Deterministic per-drone binary mission compiler and onboard layout.

Transforms canonical show trajectories into compact, chunked binary payloads
(``.tarm``) suitable for direct execution by flight controller microcontrollers
(STM32H7 / Pixhawk) with limited RAM (1-2MB).

Memory Layout Specification:
----------------------------
Each compiled mission file (``.tarm``) consists of a fixed 40-byte header
followed by N contiguous 16-byte waypoint records packed in little-endian order:

    Header (40 bytes):
      uint32_t magic           b"TMSN" (0x4E534D54)
      uint16_t format_version  1
      uint16_t flags           bit 0: records carry LED color (r, g, b)
      uint16_t drone_id        Physical target drone ID
      uint16_t reserved        0
      uint32_t waypoint_count  Number of waypoint records
      uint32_t duration_ms     Time of the final waypoint in milliseconds
      uint8_t  show_id[16]     Stable show identity (SHA-256 digest prefix)
      uint32_t records_crc32   CRC-32 checksum of all records following the header

    Waypoint Record (16 bytes each):
      uint32_t timestamp_ms    Timestamp from show start in milliseconds
      int16_t  x_cm            Local NED X position in cm (+/-327.67m)
      int16_t  y_cm            Local NED Y position in cm (+/-327.67m)
      int16_t  z_cm            Local NED Z position in cm (+/-327.67m, Z down)
      uint16_t yaw_cdeg        Yaw heading in centidegrees (0..35999, 0=North)
      uint8_t  r               Red LED intensity (0..255)
      uint8_t  g               Green LED intensity (0..255)
      uint8_t  b               Blue LED intensity (0..255)
      uint8_t  _pad            1-byte padding for 16-byte alignment

At 16 bytes per waypoint, a 15-minute show at 2Hz waypoints consumes only
~28.8 KB per drone (40 B header + 1800 * 16 B = 28,840 B), easily fitting in
onboard microcontroller RAM/flash with zero JSON parsing overhead.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .mission import (
    HEADER_SIZE,
    MISSION_FORMAT_VERSION,
    MISSION_MAGIC,
    RECORD_SIZE,
    SHOW_ID_SIZE,
    MissionHeader,
    compile_mission,
    compute_show_id,
    decode_mission,
    decode_mission_header,
)
from .show import Show

TARM_EXTENSION = ".tarm"
MANIFEST_FILENAME = "manifest.json"

HEADER_STRUCT_FORMAT = "<4sHHHHII16sI"
WAYPOINT_STRUCT_FORMAT = "<IhhhHBBBx"

C_HEADER_STRUCT = """\
#pragma pack(push, 1)
typedef struct {
    char magic[4];        /* "TMSN" */
    uint16_t version;     /* Format version (1) */
    uint16_t flags;       /* bit 0: records carry LED */
    uint16_t drone_id;    /* Physical target drone ID */
    uint16_t reserved;    /* Reserved (0) */
    uint32_t count;       /* Waypoint count */
    uint32_t duration_ms; /* Total show duration in milliseconds */
    uint8_t show_id[16];  /* 16-byte SHA-256 show digest */
    uint32_t crc32;       /* CRC-32 of following waypoint records */
} MissionHeader; /* 40 bytes */
#pragma pack(pop)
"""

C_WAYPOINT_STRUCT = """\
#pragma pack(push, 1)
typedef struct {
    uint32_t time_ms;     /* Timestamp in milliseconds */
    int16_t x_cm;         /* Local NED X in cm (+/-327.67m) */
    int16_t y_cm;         /* Local NED Y in cm (+/-327.67m) */
    int16_t z_cm;         /* Local NED Z in cm (+/-327.67m, Z down) */
    uint16_t yaw_cdeg;    /* Yaw in centidegrees [0..35999] */
    uint8_t r;            /* Red channel [0..255] */
    uint8_t g;            /* Green channel [0..255] */
    uint8_t b;            /* Blue channel [0..255] */
    uint8_t _pad;         /* 1-byte padding for 16-byte alignment */
} WaypointRecord; /* 16 bytes */
#pragma pack(pop)
"""

__all__ = [
    "C_HEADER_STRUCT",
    "C_WAYPOINT_STRUCT",
    "HEADER_SIZE",
    "HEADER_STRUCT_FORMAT",
    "MANIFEST_FILENAME",
    "MISSION_FORMAT_VERSION",
    "MISSION_MAGIC",
    "RECORD_SIZE",
    "SHOW_ID_SIZE",
    "TARM_EXTENSION",
    "WAYPOINT_STRUCT_FORMAT",
    "CompilationManifest",
    "CompiledDroneMission",
    "DroneManifestEntry",
    "compile_drone_mission",
    "compile_show",
    "compile_input_errors",
    "compute_payload_sha256",
    "decompile_drone_mission",
    "load_show",
    "write_compiled_show",
]



def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def compile_input_errors(show: Show, max_errors: int = 10) -> list[str]:
    """Describe everything about a show that makes it impossible to compile faithfully.

    The mission encoder raises on some of these with a bare Python message
    ("cannot convert float NaN to integer"), lets a LED value of 300 wrap to 44
    without a word, and the safety validators compare against NaN as if it were
    in range. This runs first, so the answer is a list of what is wrong and
    where, not a traceback or a quietly altered file.

    Checks, per drone: at least one waypoint; ``time``/``x``/``y``/``z``/``yaw``
    are finite numbers (not NaN, infinity, null or text); ``time`` is not
    negative; ``r``/``g``/``b`` (and ``w``) are whole numbers 0-255 when present.
    Missing keys are already refused by :meth:`Show.from_any`.

    Args:
        show: The show to check.
        max_errors: Stop after this many.

    Returns:
        Human-readable errors; empty means the input is structurally sound. It does
        not judge safety: that is the validators' job.
    """
    errors: list[str] = []

    def add(message: str) -> bool:
        errors.append(message)
        return len(errors) >= max_errors

    if not show.drones:
        add("the show has no drones")
        return errors

    for drone_id in sorted(show.drones):
        waypoints = show.drones[drone_id]
        if not waypoints:
            if add(f"drone {drone_id} has no waypoints"):
                return errors
            continue
        for index, wp in enumerate(waypoints):
            where = f"drone {drone_id} waypoint {index}"
            for key in ("time", "x", "y", "z", "yaw"):
                if key not in wp:
                    # time/x/y/z are guaranteed by Show.from_any; yaw is optional.
                    continue
                value = wp[key]
                if not _is_number(value):
                    if add(f"{where}: {key} is {value!r}, not a number"):
                        return errors
                elif not math.isfinite(value):
                    if add(f"{where}: {key} is {value}, not a finite number"):
                        return errors
                elif key == "time" and value < 0:
                    if add(f"{where}: time is {value}, before the show starts"):
                        return errors
            for key in ("r", "g", "b", "w"):
                if key not in wp:
                    continue
                value = wp[key]
                if not _is_number(value) or not math.isfinite(value) or value != int(value) or not 0 <= value <= 255:
                    if add(f"{where}: LED channel {key} is {value!r}, not a whole number from 0 to 255"):
                        return errors
    return errors


def compute_payload_sha256(payload: bytes) -> str:
    """Calculate the hex-encoded SHA-256 digest of a compiled binary payload."""
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class CompiledDroneMission:
    """A single drone's compiled deterministic binary mission."""

    drone_id: int
    payload: bytes
    sha256: str
    waypoint_count: int
    duration_ms: int
    records_crc32: int
    show_id: bytes

    @property
    def show_id_hex(self) -> str:
        return self.show_id.hex()

    @property
    def size_bytes(self) -> int:
        return len(self.payload)


@dataclass(frozen=True)
class DroneManifestEntry:
    """Metadata entry for one drone in the compilation manifest."""

    drone_id: int
    file_name: str
    sha256: str
    waypoint_count: int
    duration_ms: int
    records_crc32: int
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "drone_id": self.drone_id,
            "file_name": self.file_name,
            "sha256": self.sha256,
            "waypoint_count": self.waypoint_count,
            "duration_ms": self.duration_ms,
            "records_crc32": self.records_crc32,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class CompilationManifest:
    """Deterministic manifest of a compiled multi-drone show."""

    show_id: str
    drone_count: int
    duration_s: float
    total_waypoint_count: int
    total_binary_bytes: int
    manifest_sha256: str
    drones: dict[int, DroneManifestEntry]

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": MISSION_FORMAT_VERSION,
            "show_id": self.show_id,
            "drone_count": self.drone_count,
            "duration_s": self.duration_s,
            "total_waypoint_count": self.total_waypoint_count,
            "total_binary_bytes": self.total_binary_bytes,
            "manifest_sha256": self.manifest_sha256,
            "drones": {
                str(did): entry.to_dict()
                for did, entry in sorted(self.drones.items())
            },
        }


def compile_drone_mission(
    waypoints: list[dict],
    *,
    drone_id: int,
    show_id: bytes,
    origin: tuple[float, float] = (0.0, 0.0),
) -> CompiledDroneMission:
    """Compile one drone's waypoints into a deterministic binary mission payload.

    Args:
        waypoints: List of time-ordered waypoints with coordinates in meters.
        drone_id: Physical target drone ID.
        show_id: 16-byte stable show digest.
        origin: (x, y) launch pad offset in meters.

    Returns:
        CompiledDroneMission containing the binary payload and SHA-256 digest.
    """
    payload = compile_mission(waypoints, drone_id=drone_id, show_id=show_id, origin=origin)
    header = decode_mission_header(payload)
    sha256 = compute_payload_sha256(payload)
    return CompiledDroneMission(
        drone_id=drone_id,
        payload=payload,
        sha256=sha256,
        waypoint_count=header.waypoint_count,
        duration_ms=header.duration_ms,
        records_crc32=header.records_crc32,
        show_id=show_id,
    )


def load_show(source: Show | dict | str | Path) -> Show:
    """Normalize a show from a Show instance, dict, JSON string, or file path."""
    if isinstance(source, Show):
        return source
    if isinstance(source, Path):
        text = source.read_text(encoding="utf-8")
        data = json.loads(text)
        return Show.from_any(data)
    if isinstance(source, str):
        p = Path(source)
        if p.is_file():
            text = p.read_text(encoding="utf-8")
            data = json.loads(text)
            return Show.from_any(data)
        stripped = source.strip()
        if stripped.startswith("{"):
            data = json.loads(stripped)
            return Show.from_any(data)
    if isinstance(source, dict):
        return Show.from_any(source)
    raise TypeError(
        f"Cannot load show from {type(source).__name__}: expected Show, dict, or file path"
    )


def compile_show(
    show: Show | dict | str | Path,
    output_dir: Path | str | None = None,
    *,
    origins: dict[int, tuple[float, float]] | None = None,
) -> tuple[dict[int, CompiledDroneMission], CompilationManifest]:
    """Compile a full multi-drone show into per-drone deterministic .tarm payloads.

    Args:
        show: Show object, dict, JSON string, or file path.
        output_dir: Optional directory to write compiled .tarm files and manifest.json.
        origins: Optional mapping of drone_id to (x, y) pad coordinates in meters.

    Returns:
        A tuple of (missions_by_drone_id, CompilationManifest).
    """
    show_obj = load_show(show)
    show_id = compute_show_id(show_obj)

    missions: dict[int, CompiledDroneMission] = {}
    drone_entries: dict[int, DroneManifestEntry] = {}
    total_waypoints = 0
    total_bytes = 0

    for did in sorted(show_obj.drones.keys()):
        wps = show_obj.drones[did]
        origin = origins.get(did, (0.0, 0.0)) if origins else (0.0, 0.0)
        mission = compile_drone_mission(wps, drone_id=did, show_id=show_id, origin=origin)
        missions[did] = mission

        file_name = f"drone_{did}{TARM_EXTENSION}"
        entry = DroneManifestEntry(
            drone_id=did,
            file_name=file_name,
            sha256=mission.sha256,
            waypoint_count=mission.waypoint_count,
            duration_ms=mission.duration_ms,
            records_crc32=mission.records_crc32,
            size_bytes=mission.size_bytes,
        )
        drone_entries[did] = entry
        total_waypoints += mission.waypoint_count
        total_bytes += mission.size_bytes

    # Deterministic manifest SHA-256 over show_id and sorted drone hashes
    manifest_hasher = hashlib.sha256()
    manifest_hasher.update(show_id)
    for did in sorted(drone_entries.keys()):
        de = drone_entries[did]
        manifest_hasher.update(
            did.to_bytes(2, "little")
            + de.waypoint_count.to_bytes(4, "little")
            + bytes.fromhex(de.sha256)
        )
    manifest_sha256 = manifest_hasher.hexdigest()

    manifest = CompilationManifest(
        show_id=show_id.hex(),
        drone_count=len(missions),
        duration_s=show_obj.duration,
        total_waypoint_count=total_waypoints,
        total_binary_bytes=total_bytes,
        manifest_sha256=manifest_sha256,
        drones=drone_entries,
    )

    if output_dir is not None:
        write_compiled_show(missions, manifest, output_dir)

    return missions, manifest


def write_compiled_show(
    missions: dict[int, CompiledDroneMission],
    manifest: CompilationManifest,
    output_dir: Path | str,
) -> None:
    """Write each drone's ``.tarm`` and ``manifest.json`` into ``output_dir``.

    Split out of :func:`compile_show` so a caller can compile in memory, check
    the exact bytes, and only then put files on disk.
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    for did, mission in missions.items():
        (out_path / manifest.drones[did].file_name).write_bytes(mission.payload)
    (out_path / MANIFEST_FILENAME).write_text(
        json.dumps(manifest.to_dict(), indent=2) + "\n",
        encoding="utf-8",
    )


def decompile_drone_mission(blob: bytes) -> tuple[MissionHeader, list[dict]]:
    """Decode a compiled .tarm binary mission back to header and local waypoints."""
    return decode_mission(blob)
