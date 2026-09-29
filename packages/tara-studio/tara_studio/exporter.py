"""
Taramandal Studio - Trajectory Exporter (CSV, JSON, KML)
"""

import json
import csv
import io
import math
import struct
import zlib

from tara_core.show import SHOW_FORMAT_VERSION, Show, ShowContainer

def export_json(choreo_data: dict) -> str:
    """Serialises a choreography as a show file, refusing one that breaks the schema.

    Stamps ``version`` when the choreography doesn't carry one, computes the
    per-show geofence envelope (issue #53), then checks the result against
    ``schemas/show_v1.json`` (via ``ShowContainer``) before anything is written:
    a malformed show should fail here, in the Studio, rather than on upload to
    the GCS. The dict itself is what gets written, not the model's dump, so
    unknown keys and number formatting survive unchanged.

    Raises:
        pydantic.ValidationError: (a ValueError) listing every offending field
            path, e.g. ``drones.0.waypoints.12.yaw``.
    """
    data = {"version": SHOW_FORMAT_VERSION, **choreo_data}
    if isinstance(data.get("drones"), list):
        data["_geofence"] = Show.from_any(data).geofence_envelope()
    ShowContainer.model_validate(data)
    # allow_nan=False: the schema only vets the fields it knows, and a NaN in
    # an unknown key would otherwise be written out as invalid JSON.
    return json.dumps(data, indent=2, allow_nan=False)

def export_csv(choreo_data: dict) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    # Include LED colour columns (r,g,b 0-255) when the choreography carries
    # light data, so exported shows keep their colour choreography.
    has_color = any(
        "r" in wp
        for drone in choreo_data.get("drones", [])
        for wp in drone.get("waypoints", [])
    )
    header = ["drone_id", "time", "x", "y", "z", "yaw"]
    if has_color:
        header += ["r", "g", "b"]
    writer.writerow(header)

    for drone in choreo_data.get("drones", []):
        d_id = drone["id"]
        for wp in drone.get("waypoints", []):
            row = [d_id, wp["time"], wp["x"], wp["y"], wp["z"], wp["yaw"]]
            if has_color:
                row += [wp.get("r", 255), wp.get("g", 255), wp.get("b", 255)]
            writer.writerow(row)

    return output.getvalue()

def export_kml(choreo_data: dict, home_lat: float = 12.9716, home_lon: float = 77.5946) -> str:
    """
    Exports trajectories to Google Earth KML format using relativeToGround coordinates.
    Local (x, y) coordinates map to North/East GPS offsets.
    """
    # Earth radius
    R = 6378137.0
    
    kml_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        '  <Document>',
        '    <name>Taramandal Swarm Choreography Mockup</name>',
        '    <description>3D flight paths of the Taramandal Drone Show</description>',
        '    <Style id="path_style">',
        '      <LineStyle>',
        '        <color>ff00ff00</color> <!-- Green -->',
        '        <width>3</width>',
        '      </LineStyle>',
        '      <PolyStyle>',
        '        <color>2200ff00</color> <!-- Semi-transparent ground wall -->',
        '      </PolyStyle>',
        '    </Style>'
    ]
    
    for drone in choreo_data.get("drones", []):
        d_id = drone["id"]
        wps = drone.get("waypoints", [])
        
        coord_strings = []
        for wp in wps:
            # Convert local North (x) and East (y) meters to Lat/Lon offsets
            dlat = wp["x"] / R
            dlon = wp["y"] / (R * math.cos(math.radians(home_lat)))
            
            lat = home_lat + math.degrees(dlat)
            lon = home_lon + math.degrees(dlon)
            
            # Local Z is negative Down, so height above ground is -z
            height = max(0.0, -wp["z"])
            
            coord_strings.append(f"{lon:.8f},{lat:.8f},{height:.2f}")
            
        coords_block = " ".join(coord_strings)
        
        kml_parts.extend([
            '    <Placemark>',
            f'      <name>Drone {d_id:02d}</name>',
            '      <styleUrl>#path_style</styleUrl>',
            '      <LineString>',
            '        <extrude>1</extrude>',
            '        <tessellate>1</tessellate>',
            '        <altitudeMode>relativeToGround</altitudeMode>',
            f'        <coordinates>{coords_block}</coordinates>',
            '      </LineString>',
            '    </Placemark>'
        ])
        
    kml_parts.extend([
        '  </Document>',
        '</kml>'
    ])
    
    return "\n".join(kml_parts)


# ---------------------------------------------------------------------------
# Binary .tara export
# ---------------------------------------------------------------------------
#
# A compiled show is a header, a per-drone index, then fixed-width frame
# records. Fixed-width is the point: firmware seeks to a timestamp with a
# multiply rather than parsing, and a show streams off cheap flash without a
# heap allocation.
#
# Record layout, little-endian, one per drone per sampled frame:
#
#   uint32 time_ms | int16 x_cm | int16 y_cm | int16 z_cm | int16 yaw_cdeg
#   uint8 r | uint8 g | uint8 b | uint8 w
#
# That is 4+2+2+2+2+1+1+1+1 = 16 bytes with struct's default alignment, and
# TARA_RECORD_FMT below pins it to 16 explicitly ("<I4h4B"). Issue #14 calls
# this a "9-byte record" while listing these same nine fields; nine fields is
# the sense that matches the field list, so the field list is what's
# implemented. Raised on the issue thread rather than silently packing to a
# size that could not hold the listed data.

TARA_MAGIC = b"TARA"
TARA_VERSION = 1

# "<" pins little-endian AND disables struct's native padding, so this is
# exactly 16 bytes on every platform rather than whatever the C ABI would pick.
TARA_RECORD_FMT = "<I4h4B"
TARA_RECORD_SIZE = struct.calcsize(TARA_RECORD_FMT)  # 16

# magic(4) version(2) drone_count(2) frame_count(4) rate_hz(4 float)
# duration_ms(4) reserved(4) checksum(4)
TARA_HEADER_FMT = "<4sHHIfIII"
TARA_HEADER_SIZE = struct.calcsize(TARA_HEADER_FMT)  # 28

_INT16_MIN, _INT16_MAX = -32768, 32767


def _clamp_i16(value: float, field: str) -> int:
    """Rounds to int16, raising rather than silently wrapping.

    A show that overflows here would fly to a wildly wrong coordinate, so this
    is a hard error: at 1 cm resolution int16 covers +/-327.67 m, which is far
    outside any legal show volume, so hitting it means the input is wrong.
    """
    v = int(round(value))
    if not (_INT16_MIN <= v <= _INT16_MAX):
        raise ValueError(f"{field}={value} exceeds int16 range once scaled ({v})")
    return v


def _wrap_yaw_cdeg(yaw_deg: float) -> int:
    """Normalises a heading into int16 centidegrees.

    Generators emit yaw in 0..360, but 360 deg * 100 = 36000 overflows int16
    (max 32767) - anything past 327.67 deg would wrap to a negative heading and
    point the drone the wrong way. Folding into the signed -180..180 range
    first keeps every legal heading representable and matches the convention
    MAVLink already uses for attitude.
    """
    wrapped = (yaw_deg + 180.0) % 360.0 - 180.0
    return int(round(wrapped * 100.0))


def _frame_records(choreo: dict) -> tuple[list[int], list[bytes]]:
    """Packs every waypoint into records, grouped by drone.

    Returns (drone_ids, per-drone list of packed record blocks). Records are
    ordered by drone, then by time within that drone - so a player streaming
    one drone's track reads one contiguous run.
    """
    drone_ids: list[int] = []
    blocks: list[bytes] = []

    for drone in choreo.get("drones", []):
        d_id = int(drone["id"])
        if not (0 <= d_id <= 0xFFFF):
            raise ValueError(f"drone id {d_id} outside uint16 range")
        drone_ids.append(d_id)

        buf = bytearray()
        for wp in sorted(drone.get("waypoints", []), key=lambda w: w["time"]):
            buf += struct.pack(
                TARA_RECORD_FMT,
                int(round(wp["time"] * 1000.0)),
                _clamp_i16(wp["x"] * 100.0, "x"),
                _clamp_i16(wp["y"] * 100.0, "y"),
                _clamp_i16(wp["z"] * 100.0, "z"),
                _wrap_yaw_cdeg(wp.get("yaw", 0.0)),
                int(wp.get("r", 255)) & 255,
                int(wp.get("g", 255)) & 255,
                int(wp.get("b", 255)) & 255,
                int(wp.get("w", 0)) & 255,
            )
        blocks.append(bytes(buf))

    return drone_ids, blocks


def export_tara(choreo: dict, rate_hz: float = 0.0) -> bytes:
    """Compiles a choreography into the binary .tara container.

    Layout:
        header (28 B) | index (4 B per drone) | records (16 B each)

    The index holds each drone's (uint16 id, uint16 frame_count) so a player
    can locate a drone's block without scanning. The header checksum is a
    CRC32 over the index and record payload - not the header itself, which
    contains the checksum - so a truncated or corrupted file is detected
    before a single frame is flown.
    """
    drone_ids, blocks = _frame_records(choreo)

    index = bytearray()
    for d_id, block in zip(drone_ids, blocks):
        index += struct.pack("<HH", d_id, len(block) // TARA_RECORD_SIZE)

    payload = bytes(index) + b"".join(blocks)
    checksum = zlib.crc32(payload) & 0xFFFFFFFF

    total_frames = sum(len(b) // TARA_RECORD_SIZE for b in blocks)
    duration_ms = 0
    for drone in choreo.get("drones", []):
        for wp in drone.get("waypoints", []):
            duration_ms = max(duration_ms, int(round(wp["time"] * 1000.0)))

    header = struct.pack(
        TARA_HEADER_FMT,
        TARA_MAGIC,
        TARA_VERSION,
        len(drone_ids),
        total_frames,
        float(rate_hz),
        duration_ms,
        0,  # reserved: keeps the header 4-byte aligned and leaves room to grow
        checksum,
    )
    return header + payload


def parse_tara(blob: bytes) -> dict:
    """Reads a .tara container back into a dict. Inverse of export_tara().

    Exists so the exporter's own output can be verified end to end - a writer
    with no reader is a format nobody can check.
    """
    if len(blob) < TARA_HEADER_SIZE:
        raise ValueError("not a .tara file: shorter than one header")

    magic, version, drone_count, frame_count, rate_hz, duration_ms, _reserved, checksum = (
        struct.unpack(TARA_HEADER_FMT, blob[:TARA_HEADER_SIZE])
    )
    if magic != TARA_MAGIC:
        raise ValueError(f"not a .tara file: bad magic {magic!r}")
    if version != TARA_VERSION:
        raise ValueError(f"unsupported .tara version {version}")

    payload = blob[TARA_HEADER_SIZE:]
    if (zlib.crc32(payload) & 0xFFFFFFFF) != checksum:
        raise ValueError("checksum mismatch: file is corrupt or truncated")

    index_size = drone_count * 4
    index, records = payload[:index_size], payload[index_size:]

    drones = []
    offset = 0
    for i in range(drone_count):
        d_id, n_frames = struct.unpack_from("<HH", index, i * 4)
        wps = []
        for _ in range(n_frames):
            t_ms, x, y, z, yaw, r, g, b, w = struct.unpack_from(TARA_RECORD_FMT, records, offset)
            offset += TARA_RECORD_SIZE
            wps.append({
                "time": t_ms / 1000.0,
                "x": x / 100.0, "y": y / 100.0, "z": z / 100.0,
                # Re-fold to 0..360, the range generators and the GCS use.
                "yaw": (yaw / 100.0) % 360.0,
                "r": r, "g": g, "b": b, "w": w,
            })
        drones.append({"id": d_id, "waypoints": wps})

    return {
        "version": version,
        "rate_hz": rate_hz,
        "duration_ms": duration_ms,
        "frame_count": frame_count,
        "checksum": checksum,
        "drones": drones,
    }
