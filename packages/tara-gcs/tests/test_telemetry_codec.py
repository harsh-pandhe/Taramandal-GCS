"""The binary fast-frame layout is a wire contract with the frontend's decoder
(packages/tara-web/src/telemetry.js) — these tests pin the exact byte layout
down so the two sides can't silently drift apart.
"""

import struct

from tara_gcs.telemetry_codec import (
    BYTES_PER_DRONE,
    FLOATS_PER_DRONE,
    encode_fast_frame,
    encode_slow_frame,
)


def _telemetry(n):
    return {
        i: {
            "fleet_x": float(i),
            "fleet_y": float(i) * 2,
            "fleet_z": -float(i) * 3,
            "local_x": float(i) * 0.5,
            "local_y": float(i) * 0.25,
            "local_z": -float(i) * 0.75,
            "heading": float(i) * 10,
            "armed": i % 2 == 0,
            "connected": True,
            "planned_x": float(i) + 0.5,
            "planned_y": float(i) * 2 + 0.25,
            "planned_z": -float(i) * 3 - 0.75,
            "planned_valid": True,
            "flight_mode": "OFFBOARD",
            "battery_percent": 90.0 - i,
            "battery_voltage": 16.0,
            "gps_lock": True,
            "satellites": 12,
            "gps_fix_type": "FIX_3D",
            "comm_latency_ms": 5.0,
            "home_lat": 12.9,
            "home_lon": 77.5,
            "home_alt": 900.0,
            "port": 14540 + i,
        }
        for i in range(n)
    }


class TestFastFrame:
    def test_byte_length_matches_drone_count(self):
        frame = encode_fast_frame(_telemetry(10))
        assert len(frame) == 10 * BYTES_PER_DRONE

    def test_empty_fleet_is_empty_bytes(self):
        assert encode_fast_frame({}) == b""

    def test_values_round_trip_in_sorted_drone_order(self):
        frame = encode_fast_frame(_telemetry(3))
        floats = struct.unpack(f"<{3 * FLOATS_PER_DRONE}f", frame)
        # drone 0: fleet xyz=0,0,0 local xyz=0,0,0 heading=0 armed=1 connected=1,
        # then planned xyz=0.5,0.25,-0.75 and planned_valid=1
        assert floats[0:13] == (
            0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.5, 0.25, -0.75, 1.0,
        )
        # drone 1: fleet xyz=1,2,-3 local xyz=0.5,0.25,-0.75 heading=10
        # armed=0(odd) connected=1, planned xyz=1.5,2.25,-3.75, valid=1
        assert floats[13:26] == (
            1.0, 2.0, -3.0, 0.5, 0.25, -0.75, 10.0, 0.0, 1.0, 1.5, 2.25, -3.75, 1.0,
        )

    def test_missing_fields_default_to_zero(self):
        frame = encode_fast_frame({0: {}})
        floats = struct.unpack(f"<{FLOATS_PER_DRONE}f", frame)
        assert floats == (0.0,) * FLOATS_PER_DRONE

    def test_scales_to_100_drones(self):
        frame = encode_fast_frame(_telemetry(100))
        assert len(frame) == 100 * BYTES_PER_DRONE
        # ~5.2KB at 100 drones, not several hundred KB of JSON.
        assert len(frame) < 6000

    def test_drone_order_is_by_sorted_id_not_insertion_order(self):
        telemetry = {5: {"fleet_x": 5.0}, 1: {"fleet_x": 1.0}, 3: {"fleet_x": 3.0}}
        frame = encode_fast_frame(telemetry)
        floats = struct.unpack(f"<{3 * FLOATS_PER_DRONE}f", frame)
        assert (
            floats[0],
            floats[FLOATS_PER_DRONE],
            floats[2 * FLOATS_PER_DRONE],
        ) == (1.0, 3.0, 5.0)


class TestSlowFrame:
    def test_has_a_type_tag_for_client_side_dispatch(self):
        assert encode_slow_frame(_telemetry(1))["type"] == "slow"

    def test_contains_every_slow_field_but_not_fast_fields(self):
        payload = encode_slow_frame(_telemetry(1))
        drone = payload["drones"]["0"]
        assert set(drone) == {
            "flight_mode", "battery_percent", "battery_voltage", "gps_lock",
            "satellites", "gps_fix_type", "comm_latency_ms",
            "home_lat", "home_lon", "home_alt", "port", "aborted",
        }
        assert "fleet_x" not in drone
        assert "armed" not in drone

    def test_keyed_by_string_drone_id_for_json_object_compatibility(self):
        payload = encode_slow_frame(_telemetry(2))
        assert set(payload["drones"]) == {"0", "1"}

    def test_json_serializable(self):
        import json

        json.dumps(encode_slow_frame(_telemetry(5)))


class TestPlannedPosition:
    """The deviation HUD's wire contract: the planned point ships alongside the
    measured one, so the client can score |actual - planned| without re-deriving
    the trajectory interpolation itself.
    """

    def test_planned_fields_default_to_zero_and_invalid(self):
        # A fleet that isn't flying a trajectory has no planned point. The flag
        # must read false so the client shows "unmonitored" rather than a
        # perfect-zero deviation against a planned position of (0, 0, 0).
        frame = encode_fast_frame({0: {"fleet_x": 12.0}})
        floats = struct.unpack(f"<{FLOATS_PER_DRONE}f", frame)
        assert floats[9:13] == (0.0, 0.0, 0.0, 0.0)

    def test_planned_valid_is_encoded_as_a_float_flag(self):
        frame = encode_fast_frame({0: {"planned_valid": True}})
        floats = struct.unpack(f"<{FLOATS_PER_DRONE}f", frame)
        assert floats[12] == 1.0

    def test_planned_position_round_trips(self):
        telemetry = {
            0: {
                "fleet_x": 10.0, "fleet_y": 20.0, "fleet_z": 30.0,
                "planned_x": 10.5, "planned_y": 19.0, "planned_z": 30.25,
                "planned_valid": True,
            }
        }
        floats = struct.unpack(f"<{FLOATS_PER_DRONE}f", encode_fast_frame(telemetry))
        assert floats[9:12] == (10.5, 19.0, 30.25)

    def test_planned_fields_are_not_duplicated_on_the_slow_channel(self):
        # Deviation has to update at the fast channel's rate to be useful; if it
        # also rode the 1Hz JSON channel the two would disagree between ticks.
        drone = encode_slow_frame(_telemetry(1))["drones"]["0"]
        assert not any(field.startswith("planned_") for field in drone)
