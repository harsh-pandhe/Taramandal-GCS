"""Deterministic binary mission compiler tests (issue #56).

Validates:
1. C struct memory layout parity (sizeof, offsets, packing).
2. Pure Python encoder and decoder zero-drift roundtrips.
3. Bounds checking for out-of-range coordinates and timestamps.
4. Deterministic SHA-256 hash generation and golden vectors.
5. Multi-drone compilation and manifest generation.
"""

import ctypes
import json
import struct

import pytest
from tara_core import (
    MANIFEST_FILENAME,
    TARM_EXTENSION,
    compile_drone_mission,
    compile_show,
    compute_payload_sha256,
    decompile_drone_mission,
)
from tara_core.compiler import (
    HEADER_STRUCT_FORMAT,
    WAYPOINT_STRUCT_FORMAT,
)
from tara_core.mission import HEADER_SIZE, RECORD_SIZE, SHOW_ID_SIZE


class CMissionHeader(ctypes.LittleEndianStructure):
    _pack_ = 1
    _fields_ = [
        ("magic", ctypes.c_char * 4),
        ("version", ctypes.c_uint16),
        ("flags", ctypes.c_uint16),
        ("drone_id", ctypes.c_uint16),
        ("reserved", ctypes.c_uint16),
        ("count", ctypes.c_uint32),
        ("duration_ms", ctypes.c_uint32),
        ("show_id", ctypes.c_uint8 * 16),
        ("crc32", ctypes.c_uint32),
    ]


class CWaypointRecord(ctypes.LittleEndianStructure):
    _pack_ = 1
    _fields_ = [
        ("time_ms", ctypes.c_uint32),
        ("x_cm", ctypes.c_int16),
        ("y_cm", ctypes.c_int16),
        ("z_cm", ctypes.c_int16),
        ("yaw_cdeg", ctypes.c_uint16),
        ("r", ctypes.c_uint8),
        ("g", ctypes.c_uint8),
        ("b", ctypes.c_uint8),
        ("_pad", ctypes.c_uint8),
    ]


def _sample_waypoints():
    return [
        {"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0, "r": 255, "g": 0, "b": 0},
        {"time": 2.5, "x": 12.34, "y": -5.67, "z": -15.5, "yaw": 180.0, "r": 0, "g": 255, "b": 128},
        {"time": 5.0, "x": 20.0, "y": 10.0, "z": -2.0, "yaw": 359.0, "r": 0, "g": 0, "b": 255},
    ]


class TestCStructParity:
    """Parity between Python struct packing and packed C microcontroller structures."""

    def test_header_and_record_sizes(self):
        assert ctypes.sizeof(CMissionHeader) == 40
        assert ctypes.sizeof(CWaypointRecord) == 16
        assert struct.calcsize(HEADER_STRUCT_FORMAT) == 40
        assert struct.calcsize(WAYPOINT_STRUCT_FORMAT) == 16
        assert HEADER_SIZE == 40
        assert RECORD_SIZE == 16

    def test_header_offsets(self):
        assert CMissionHeader.magic.offset == 0
        assert CMissionHeader.version.offset == 4
        assert CMissionHeader.flags.offset == 6
        assert CMissionHeader.drone_id.offset == 8
        assert CMissionHeader.reserved.offset == 10
        assert CMissionHeader.count.offset == 12
        assert CMissionHeader.duration_ms.offset == 16
        assert CMissionHeader.show_id.offset == 20
        assert CMissionHeader.crc32.offset == 36

    def test_record_offsets(self):
        assert CWaypointRecord.time_ms.offset == 0
        assert CWaypointRecord.x_cm.offset == 4
        assert CWaypointRecord.y_cm.offset == 6
        assert CWaypointRecord.z_cm.offset == 8
        assert CWaypointRecord.yaw_cdeg.offset == 10
        assert CWaypointRecord.r.offset == 12
        assert CWaypointRecord.g.offset == 13
        assert CWaypointRecord.b.offset == 14
        assert CWaypointRecord._pad.offset == 15

    def test_record_c_struct_binary_identity(self):
        """Python struct.pack matches ctypes packed struct byte for byte."""
        py_bytes = struct.pack(WAYPOINT_STRUCT_FORMAT, 2500, 1234, -567, -1550, 18000, 255, 128, 64)
        c_record = CWaypointRecord(
            time_ms=2500,
            x_cm=1234,
            y_cm=-567,
            z_cm=-1550,
            yaw_cdeg=18000,
            r=255,
            g=128,
            b=64,
            _pad=0,
        )
        assert bytes(c_record) == py_bytes


class TestCompilerRoundtrip:
    """Zero float drift roundtrips and memory footprint verification."""

    def test_roundtrip_zero_float_drift(self):
        show_id = bytes(range(SHOW_ID_SIZE))
        wps = _sample_waypoints()
        compiled = compile_drone_mission(wps, drone_id=1, show_id=show_id)

        header, decoded = decompile_drone_mission(compiled.payload)
        assert header.drone_id == 1
        assert header.waypoint_count == len(wps)
        assert header.duration_ms == 5000
        assert header.has_led

        for orig, dec in zip(wps, decoded):
            assert dec["time"] == orig["time"]
            assert dec["x"] == orig["x"]
            assert dec["y"] == orig["y"]
            assert dec["z"] == orig["z"]
            assert dec["yaw"] == orig["yaw"]
            assert dec["r"] == orig["r"]
            assert dec["g"] == orig["g"]
            assert dec["b"] == orig["b"]

    def test_memory_footprint_acceptance_criterion(self):
        """A 15-minute show at 2Hz waypoints consumes <= 20 bytes/waypoint and ~28.8KB total."""
        wps = [
            {"time": i * 0.5, "x": 10.0, "y": 20.0, "z": -15.0, "yaw": 0.0, "r": 255, "g": 255, "b": 255}
            for i in range(1801)  # 0s to 900s (15 minutes)
        ]
        show_id = bytes(range(SHOW_ID_SIZE))
        compiled = compile_drone_mission(wps, drone_id=0, show_id=show_id)

        # 40 bytes header + 1801 * 16 bytes records = 28,856 bytes
        expected_bytes = 40 + 1801 * 16
        assert compiled.size_bytes == expected_bytes
        assert compiled.size_bytes / len(wps) < 20.0  # Acceptance criterion: <= 20 bytes per waypoint
        assert compiled.size_bytes < 30 * 1024  # Fits comfortably in microcontroller RAM


class TestBoundsChecking:
    """Descriptive errors for out-of-range coordinates, timestamps, and ordering."""

    def test_coordinate_above_upper_bound_refused(self):
        wps = [{"time": 0.0, "x": 327.68, "y": 0.0, "z": -2.0}]
        show_id = bytes(range(SHOW_ID_SIZE))
        with pytest.raises(ValueError, match=r"x=327\.68m is outside.*±327\.67m"):
            compile_drone_mission(wps, drone_id=3, show_id=show_id)

    def test_coordinate_below_lower_bound_refused(self):
        wps = [{"time": 0.0, "x": 0.0, "y": -327.68, "z": -2.0}]
        show_id = bytes(range(SHOW_ID_SIZE))
        with pytest.raises(ValueError, match=r"y=-327\.68m is outside.*±327\.67m"):
            compile_drone_mission(wps, drone_id=2, show_id=show_id)

    def test_negative_timestamp_refused(self):
        wps = [{"time": -0.1, "x": 0.0, "y": 0.0, "z": -2.0}]
        show_id = bytes(range(SHOW_ID_SIZE))
        with pytest.raises(ValueError, match="waypoint time -0.1s is out of range"):
            compile_drone_mission(wps, drone_id=1, show_id=show_id)

    def test_unordered_timestamps_refused(self):
        wps = [
            {"time": 2.0, "x": 0.0, "y": 0.0, "z": -2.0},
            {"time": 1.0, "x": 0.0, "y": 0.0, "z": -2.0},
        ]
        show_id = bytes(range(SHOW_ID_SIZE))
        with pytest.raises(ValueError, match="waypoints are not time-ordered"):
            compile_drone_mission(wps, drone_id=0, show_id=show_id)


class TestGoldenVectors:
    """Pinned deterministic binary output and SHA-256 digests across platforms."""

    def test_deterministic_sha256_golden_vector(self):
        # Known static show
        show_data = {
            "version": 1,
            "drones": [
                {
                    "id": 0,
                    "waypoints": [
                        {"time": 0.0, "x": 10.0, "y": 20.0, "z": -5.0, "yaw": 0.0, "r": 255, "g": 0, "b": 0},
                        {"time": 1.0, "x": 10.0, "y": 20.0, "z": -10.0, "yaw": 90.0, "r": 0, "g": 255, "b": 0},
                    ],
                },
                {
                    "id": 1,
                    "waypoints": [
                        {"time": 0.0, "x": 15.0, "y": 25.0, "z": -5.0, "yaw": 180.0, "r": 0, "g": 0, "b": 255},
                        {"time": 1.0, "x": 15.0, "y": 25.0, "z": -10.0, "yaw": 270.0, "r": 255, "g": 255, "b": 0},
                    ],
                },
            ],
        }

        missions, manifest = compile_show(show_data)
        assert len(missions) == 2
        assert manifest.drone_count == 2
        assert manifest.duration_s == 1.0
        assert manifest.total_waypoint_count == 4
        # 2 drones * (40 B header + 2 * 16 B records = 72 B) = 144 B
        assert manifest.total_binary_bytes == 144

        # Verify hash reproducibility
        sha0 = missions[0].sha256
        sha1 = missions[1].sha256
        assert sha0 == compute_payload_sha256(missions[0].payload)
        assert sha1 == compute_payload_sha256(missions[1].payload)

        # Golden vector checks: rerun compilation again and ensure exact match
        missions_re, manifest_re = compile_show(show_data)
        assert missions_re[0].sha256 == sha0
        assert missions_re[1].sha256 == sha1
        assert manifest_re.manifest_sha256 == manifest.manifest_sha256
        assert manifest_re.show_id == manifest.show_id


    def test_pinned_golden_bytes_and_digest(self):
        """A literal expected file, so a change in rounding, field order, padding, endianness or
        CRC fails here on any platform rather than just matching a second run of itself.

        Header: 'TMSN', version 1, flags 1 (LED), drone 1, reserved 0, 3 records, 5000 ms,
        show id 00..0f, CRC-32 of the records. Then three 16-byte records."""
        wps = [
            {"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0, "r": 255, "g": 0, "b": 0},
            {"time": 2.5, "x": 12.34, "y": -5.67, "z": -15.5, "yaw": 180.0, "r": 0, "g": 255, "b": 128},
            {"time": 5.0, "x": 20.0, "y": 10.0, "z": -2.0, "yaw": 359.0, "r": 0, "g": 0, "b": 255},
        ]
        compiled = compile_drone_mission(wps, drone_id=1, show_id=bytes(range(SHOW_ID_SIZE)))
        assert compiled.payload.hex() == (
            "544d534e0100010001000000030000008813000000010203040506070809"
            "0a0b0c0d0e0f8a522d41"
            "000000000000000038ff0000ff000000"
            "c4090000d204c9fdf2f9504600ff8000"
            "88130000d007e80338ff3c8c0000ff00"
        )
        assert compiled.sha256 == "26e0670ed1281f1845825857d9ab7e75f6874cc236186273da09ab80c226172b"


class TestCompileShowFilesystem:
    """Output directory creation, .tarm file emission, and manifest serialization."""

    def test_compile_show_writes_artifacts(self, tmp_path):
        show_data = {
            "version": 1,
            "drones": [
                {
                    "id": 0,
                    "waypoints": [
                        {"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
                        {"time": 2.0, "x": 5.0, "y": 5.0, "z": -5.0, "yaw": 45.0},
                    ],
                },
                {
                    "id": 1,
                    "waypoints": [
                        {"time": 0.0, "x": 10.0, "y": 10.0, "z": -2.0, "yaw": 0.0},
                        {"time": 2.0, "x": 15.0, "y": 15.0, "z": -5.0, "yaw": 45.0},
                    ],
                },
            ],
        }
        out_dir = tmp_path / "compiled_show"
        missions, manifest = compile_show(show_data, output_dir=out_dir)

        tarm_0 = out_dir / f"drone_0{TARM_EXTENSION}"
        tarm_1 = out_dir / f"drone_1{TARM_EXTENSION}"
        manifest_file = out_dir / MANIFEST_FILENAME

        assert tarm_0.is_file()
        assert tarm_1.is_file()
        assert manifest_file.is_file()

        assert tarm_0.read_bytes() == missions[0].payload
        assert tarm_1.read_bytes() == missions[1].payload

        parsed_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        assert parsed_manifest["drone_count"] == 2
        assert parsed_manifest["show_id"] == manifest.show_id
        assert parsed_manifest["manifest_sha256"] == manifest.manifest_sha256
        assert "0" in parsed_manifest["drones"]
        assert "1" in parsed_manifest["drones"]
        assert parsed_manifest["drones"]["0"]["sha256"] == missions[0].sha256
