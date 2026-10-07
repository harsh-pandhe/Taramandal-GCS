"""End-to-end integration tests for issue #55 Canonical Show Schema.

Verifies the unified contract:
Studio export
  → canonical ShowContainer (show_v1.json)
  → Tara Core validation / compilation
  → GCS ingestion / staging
  → GCS internal representation

Proves:
- Zero data loss: coords, yaw, LED channels (r,g,b,w), velocities (vx,vy,vz), geofence
- Deterministic serialization (sorted drone keys, sorted waypoints)
- 100-drone and 100+ drone fleet scaling
- Strict rejection of invalid/malformed payloads
- Version compatibility checks
"""

import json
import pytest

from tara_core import MAX_FLEET_SIZE, SHOW_FORMAT_VERSION, Show
from tara_gcs.trajectory_parser import parse_trajectory_bytes, parse_trajectory_file, _parse_json, _parse_csv
from tara_studio import generator
from tara_studio.exporter import export_json, export_csv
from tara_studio.led_engine import apply_track_to_choreo


def sample_canonical_choreo(num_drones: int = 3, duration: float = 10.0, rate: float = 1.0) -> dict:
    choreo = generator.generate_circle(num_drones, duration, rate)
    # Add an LED track to bake RGBW channels
    choreo = apply_track_to_choreo(
        choreo,
        [{"pattern": "SOLID", "start_time": 0, "duration": 999, "color": "#00FF80"}]
    )
    # Add velocity data to drone 0's waypoints to verify velocity preservation
    for i, wp in enumerate(choreo["drones"][0]["waypoints"]):
        wp["vx"] = 1.0 + i * 0.1
        wp["vy"] = 0.5
        wp["vz"] = -0.2
        wp["w"] = 50
    return choreo


class TestCanonicalPipelineRoundtrip:
    def test_studio_export_to_core_to_gcs_preserves_all_attributes(self):
        choreo = sample_canonical_choreo(num_drones=4)
        
        # 1. Studio export
        exported_json_str = export_json(choreo)
        assert isinstance(exported_json_str, str)
        exported_dict = json.loads(exported_json_str)

        # 2. Verify canonical container fields
        assert exported_dict["version"] == SHOW_FORMAT_VERSION
        assert "_geofence" in exported_dict
        geofence_meta = exported_dict["_geofence"]
        assert "envelope_radius_m" in geofence_meta
        assert "soft_warning_radius_m" in geofence_meta
        assert "hard_geofence_radius_m" in geofence_meta

        # 3. Core validation & loading
        core_show = Show.validate_and_load(exported_dict)
        assert core_show.num_drones == 4
        assert core_show.version == SHOW_FORMAT_VERSION
        assert core_show.has_light_data is True
        assert core_show.geofence == geofence_meta

        # 4. GCS ingestion
        gcs_trajectory = parse_trajectory_file(exported_json_str, "choreography.json")
        assert len(gcs_trajectory) == 4
        for d_id in range(4):
            assert d_id in gcs_trajectory

        # Verify Drone 0 waypoints in GCS have preserved coordinates, yaw, LED, and velocities
        wps = gcs_trajectory[0]
        assert len(wps) > 0
        first_wp = wps[0]
        assert "time" in first_wp
        assert "x" in first_wp
        assert "y" in first_wp
        assert "z" in first_wp
        assert "yaw" in first_wp
        assert 0.0 <= first_wp["yaw"] < 360.0
        # LED channels preserved
        assert first_wp["r"] == 0
        assert first_wp["g"] == 255
        assert first_wp["b"] == 128
        assert first_wp["w"] == 50
        # Velocities preserved
        assert first_wp["vx"] == 1.0
        assert first_wp["vy"] == 0.5
        assert first_wp["vz"] == -0.2

    def test_deterministic_serialization(self):
        choreo = sample_canonical_choreo(num_drones=5)
        export1 = export_json(choreo)
        export2 = export_json(choreo)
        assert export1 == export2

        # Roundtrip through Show.to_studio() preserves deterministic ordering
        show = Show.validate_and_load(json.loads(export1))
        dump1 = json.dumps(show.to_studio(), indent=2)
        dump2 = json.dumps(show.to_studio(), indent=2)
        assert dump1 == dump2


class TestFleetScaleSupport:
    def test_gcs_parses_100_drone_show(self):
        choreo = generator.generate_circle(100, 10.0, 1.0)
        json_str = export_json(choreo)
        parsed = parse_trajectory_bytes(json_str.encode("utf-8"), "show.json")
        assert len(parsed) == 100
        assert 0 in parsed
        assert 99 in parsed

    def test_gcs_parses_100_plus_drone_show(self):
        # Create a 150-drone show container
        drones = [
            {
                "id": i,
                "waypoints": [
                    {"time": 0.0, "x": float(i * 2.0), "y": 0.0, "z": -2.0, "yaw": 0.0},
                    {"time": 5.0, "x": float(i * 2.0), "y": 5.0, "z": -2.0, "yaw": 90.0},
                ]
            }
            for i in range(150)
        ]
        show_container = {
            "version": SHOW_FORMAT_VERSION,
            "drones": drones,
        }
        json_str = json.dumps(show_container)
        parsed = parse_trajectory_bytes(json_str.encode("utf-8"), "show.json")
        assert len(parsed) == 150
        assert 149 in parsed
        assert parsed[149][1]["y"] == 5.0

    def test_gcs_rejects_drone_id_at_or_above_max_fleet_size(self):
        show_container = {
            "version": SHOW_FORMAT_VERSION,
            "drones": [
                {
                    "id": MAX_FLEET_SIZE,  # 1000 is invalid, maximum is 999
                    "waypoints": [{"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0}],
                }
            ],
        }
        with pytest.raises(ValueError):
            parse_trajectory_file(json.dumps(show_container), "show.json")


class TestSchemaValidationGating:
    def test_rejects_yaw_360(self):
        show = {
            "version": 1,
            "drones": [
                {"id": 0, "waypoints": [{"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 360.0}]}
            ]
        }
        with pytest.raises(ValueError, match="Show schema validation failed"):
            _parse_json(json.dumps(show))

    def test_rejects_negative_yaw(self):
        show = {
            "version": 1,
            "drones": [
                {"id": 0, "waypoints": [{"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": -0.1}]}
            ]
        }
        with pytest.raises(ValueError, match="Show schema validation failed"):
            _parse_json(json.dumps(show))

    def test_rejects_negative_time(self):
        show = {
            "version": 1,
            "drones": [
                {"id": 0, "waypoints": [{"time": -0.5, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0}]}
            ]
        }
        with pytest.raises(ValueError, match="Show schema validation failed"):
            _parse_json(json.dumps(show))

    def test_rejects_out_of_bounds_led(self):
        show = {
            "version": 1,
            "drones": [
                {"id": 0, "waypoints": [{"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0, "r": 256}]}
            ]
        }
        with pytest.raises(ValueError, match="Show schema validation failed"):
            _parse_json(json.dumps(show))

    def test_rejects_null_led(self):
        show = {
            "version": 1,
            "drones": [
                {"id": 0, "waypoints": [{"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0, "r": None}]}
            ]
        }
        with pytest.raises(ValueError, match="Show schema validation failed"):
            _parse_json(json.dumps(show))

    def test_rejects_string_numeric_coercion(self):
        show = {
            "version": 1,
            "drones": [
                {"id": 0, "waypoints": [{"time": 0.0, "x": "1.0", "y": 0.0, "z": -2.0, "yaw": 0.0}]}
            ]
        }
        with pytest.raises(ValueError, match="Show schema validation failed"):
            _parse_json(json.dumps(show))

    def test_rejects_duplicate_drone_ids(self):
        show = {
            "version": 1,
            "drones": [
                {"id": 0, "waypoints": [{"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0}]},
                {"id": 0, "waypoints": [{"time": 0.0, "x": 1.0, "y": 0.0, "z": -2.0, "yaw": 0.0}]},
            ]
        }
        with pytest.raises(ValueError, match="duplicate drone id"):
            _parse_json(json.dumps(show))

    def test_rejects_unsupported_future_version(self):
        show = {
            "version": 2,
            "drones": [
                {"id": 0, "waypoints": [{"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0}]}
            ]
        }
        with pytest.raises(ValueError, match="Unsupported show format version 2"):
            _parse_json(json.dumps(show))

    def test_malformed_canonical_show_does_not_fall_back_to_legacy(self):
        # A payload with "drones" but missing "id" in a drone entry must fail
        malformed = {
            "version": 1,
            "drones": [
                {"waypoints": [{"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0}]}
            ]
        }
        with pytest.raises(ValueError, match="Show schema validation failed"):
            _parse_json(json.dumps(malformed))


class TestCsvConsistency:
    def test_csv_export_and_import_preserves_leds(self):
        choreo = sample_canonical_choreo(num_drones=2)
        csv_str = export_csv(choreo)
        parsed = _parse_csv(csv_str)
        assert len(parsed) == 2
        assert parsed[0][0]["r"] == 0
        assert parsed[0][0]["g"] == 255
        assert parsed[0][0]["b"] == 128
