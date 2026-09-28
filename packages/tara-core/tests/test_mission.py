"""The on-drone mission file and broadcast trigger (issue #47).

Both are wire formats a drone parses with fixed offsets, so what is pinned here
is behaviour firmware depends on: exact sizes, round-tripping, and — the
safety half — that a truncated, corrupted or mismatched file or frame is
refused rather than flown.
"""

import json
import struct
import zlib

import pytest

from tara_core import (
    ShowCommand,
    compile_mission,
    compute_show_id,
    decode_mission,
    decode_show_trigger,
    encode_show_trigger,
    mission_crc32,
)
from tara_core.mission import HEADER_SIZE, RECORD_SIZE, SHOW_ID_SIZE, TRIGGER_SIZE, decode_mission_header

SHOW_ID = bytes(range(SHOW_ID_SIZE))


def _wps():
    return [
        {"time": 0.0, "x": 10.0, "y": 4.0, "z": 0.0, "yaw": 0.0},
        {"time": 2.5, "x": 12.34, "y": 4.0, "z": -5.67, "yaw": 350.0},
        {"time": 6.0, "x": 10.0, "y": 6.5, "z": -5.0, "yaw": 10.0},
    ]


class TestLayout:
    def test_header_and_record_sizes_are_fixed(self):
        # Firmware reads these with hard-coded offsets.
        assert HEADER_SIZE == 40
        assert RECORD_SIZE == 16
        assert TRIGGER_SIZE == 29

    def test_file_size_is_header_plus_one_record_per_waypoint(self):
        blob = compile_mission(_wps(), drone_id=3, show_id=SHOW_ID)
        assert len(blob) == HEADER_SIZE + 3 * RECORD_SIZE

    def test_far_more_compact_than_json(self):
        wps = [{"time": t * 0.1, "x": 1.0 + t * 0.01, "y": 2.0, "z": -3.0, "yaw": 45.0, "r": 255, "g": 0, "b": 128}
               for t in range(600)]
        blob = compile_mission(wps, drone_id=0, show_id=SHOW_ID)
        assert len(blob) < len(json.dumps(wps)) / 4


class TestRoundTrip:
    def test_header_fields(self):
        header, _ = decode_mission(compile_mission(_wps(), drone_id=7, show_id=SHOW_ID))
        assert header.drone_id == 7
        assert header.waypoint_count == 3
        assert header.duration_ms == 6000
        assert header.show_id == SHOW_ID
        assert not header.has_led

    def test_waypoints_round_trip_to_centimetre_precision(self):
        _, wps = decode_mission(compile_mission(_wps(), drone_id=0, show_id=SHOW_ID))
        for got, want in zip(wps, _wps()):
            for key in ("time", "x", "y", "z", "yaw"):
                assert got[key] == pytest.approx(want[key], abs=0.005)

    def test_origin_is_subtracted_into_the_drones_own_frame(self):
        _, wps = decode_mission(compile_mission(_wps(), drone_id=0, show_id=SHOW_ID, origin=(10.0, 4.0)))
        assert (wps[0]["x"], wps[0]["y"], wps[0]["z"]) == (0.0, 0.0, 0.0)
        assert wps[1]["x"] == pytest.approx(2.34)
        # z has no pad offset
        assert wps[1]["z"] == pytest.approx(-5.67)

    def test_led_channels_carried_when_present(self):
        wps = [dict(wp, r=255, g=16, b=0) for wp in _wps()]
        header, decoded = decode_mission(compile_mission(wps, drone_id=0, show_id=SHOW_ID))
        assert header.has_led
        assert (decoded[1]["r"], decoded[1]["g"], decoded[1]["b"]) == (255, 16, 0)

    def test_no_led_keys_when_show_has_no_light_data(self):
        _, decoded = decode_mission(compile_mission(_wps(), drone_id=0, show_id=SHOW_ID))
        assert "r" not in decoded[0]

    def test_yaw_is_wrapped_into_0_360(self):
        wps = [{"time": 0.0, "x": 0, "y": 0, "z": 0, "yaw": -90.0}]
        _, decoded = decode_mission(compile_mission(wps, drone_id=0, show_id=SHOW_ID))
        assert decoded[0]["yaw"] == pytest.approx(270.0)


class TestCompileRefuses:
    def test_empty_waypoints(self):
        with pytest.raises(ValueError, match="empty"):
            compile_mission([], drone_id=0, show_id=SHOW_ID)

    def test_unordered_waypoints(self):
        wps = _wps()
        wps[1]["time"] = 9.0
        with pytest.raises(ValueError, match="time-ordered"):
            compile_mission(wps, drone_id=0, show_id=SHOW_ID)

    def test_position_outside_int16_centimetre_range(self):
        wps = [{"time": 0.0, "x": 400.0, "y": 0, "z": 0}]
        with pytest.raises(ValueError, match="range"):
            compile_mission(wps, drone_id=0, show_id=SHOW_ID)

    def test_wrong_size_show_id(self):
        with pytest.raises(ValueError, match="show_id"):
            compile_mission(_wps(), drone_id=0, show_id=b"short")


class TestDecodeRefuses:
    def test_flipped_record_bit_fails_crc(self):
        blob = bytearray(compile_mission(_wps(), drone_id=0, show_id=SHOW_ID))
        blob[HEADER_SIZE + 5] ^= 0x01
        with pytest.raises(ValueError, match="CRC"):
            decode_mission(bytes(blob))

    def test_truncated_upload(self):
        blob = compile_mission(_wps(), drone_id=0, show_id=SHOW_ID)
        with pytest.raises(ValueError, match="record bytes"):
            decode_mission(blob[:-1])

    def test_shorter_than_header(self):
        with pytest.raises(ValueError, match="shorter"):
            decode_mission(b"TMSN")

    def test_not_a_mission_file(self):
        blob = b"XXXX" + compile_mission(_wps(), drone_id=0, show_id=SHOW_ID)[4:]
        with pytest.raises(ValueError, match="magic"):
            decode_mission(blob)

    def test_unknown_version(self):
        blob = bytearray(compile_mission(_wps(), drone_id=0, show_id=SHOW_ID))
        struct.pack_into("<H", blob, 4, 99)
        with pytest.raises(ValueError, match="version"):
            decode_mission_header(bytes(blob))


class TestShowId:
    def _show(self):
        return {0: _wps(), 1: [dict(wp, x=wp["x"] + 2.0) for wp in _wps()]}

    def test_stable_and_sized(self):
        assert compute_show_id(self._show()) == compute_show_id(self._show())
        assert len(compute_show_id(self._show())) == SHOW_ID_SIZE

    def test_same_for_studio_and_gcs_shapes(self):
        gcs = self._show()
        studio = {"drones": [{"id": k, "waypoints": v} for k, v in gcs.items()]}
        assert compute_show_id(gcs) == compute_show_id(studio)

    def test_insensitive_to_sub_centimetre_float_noise(self):
        noisy = self._show()
        noisy[0][1]["x"] += 1e-4
        assert compute_show_id(noisy) == compute_show_id(self._show())

    def test_changes_when_any_drone_moves_a_centimetre(self):
        moved = self._show()
        moved[1][2]["z"] -= 0.01
        assert compute_show_id(moved) != compute_show_id(self._show())

    def test_changes_when_slots_are_swapped(self):
        show = self._show()
        swapped = {0: show[1], 1: show[0]}
        assert compute_show_id(swapped) != compute_show_id(show)


class TestMissionCrc:
    def test_any_byte_change_changes_the_crc(self):
        blob = compile_mission(_wps(), drone_id=0, show_id=SHOW_ID)
        other = compile_mission(_wps(), drone_id=1, show_id=SHOW_ID)
        assert mission_crc32(blob) != mission_crc32(other)


class TestTrigger:
    def test_round_trip(self):
        frame = encode_show_trigger(ShowCommand.START, SHOW_ID, 1_700_000_000_123_456)
        assert len(frame) == TRIGGER_SIZE
        decoded = decode_show_trigger(frame)
        assert decoded == {
            "command": ShowCommand.START,
            "show_id": SHOW_ID,
            "start_utc_usec": 1_700_000_000_123_456,
            "valid": True,
        }

    def test_corrupted_frame_is_invalid_not_acted_on(self):
        frame = bytearray(encode_show_trigger(ShowCommand.START, SHOW_ID, 5))
        frame[0] = ShowCommand.CANCEL  # a garbled START that decodes as CANCEL
        assert decode_show_trigger(bytes(frame))["valid"] is False

    def test_unknown_command_is_invalid(self):
        body = struct.pack("<B16sQ", 99, SHOW_ID, 0)
        frame = body + struct.pack("<I", zlib.crc32(body))
        assert decode_show_trigger(frame)["valid"] is False

    def test_wrong_size_raises(self):
        with pytest.raises(ValueError):
            decode_show_trigger(b"\x00" * (TRIGGER_SIZE - 1))

    def test_refuses_to_encode_a_bad_show_id(self):
        with pytest.raises(ValueError):
            encode_show_trigger(ShowCommand.START, b"x", 0)
