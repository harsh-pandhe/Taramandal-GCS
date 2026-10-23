"""The Studio's JSON export refuses shows that break the show schema (issue #55)."""

import json

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from tara_core import DEFAULT_LIMITS, SHOW_FORMAT_VERSION, SHOW_SCHEMA_PATH, Show
from tara_core.validate import collision_errors
from tara_studio import generator
from tara_studio.exporter import export_json
from tara_studio.led_engine import apply_track_to_choreo
from tara_studio.server import app

SHAPES = ["circle", "helix", "heart", "cube", "spline"]


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.fixture(scope="module")
def schema_validator():
    return Draft202012Validator(json.loads(SHOW_SCHEMA_PATH.read_text()))


def lit_circle() -> dict:
    """The real Studio pipeline: generate, then bake an LED track onto it."""
    choreo = generator.generate_circle(2, 10.0, 1.0)
    return apply_track_to_choreo(
        choreo, [{"pattern": "SOLID", "start_time": 0, "duration": 999, "color": "#FF8000"}]
    )


class TestRealExports:
    def test_generator_plus_led_track_export_matches_schema(self, schema_validator):
        exported = json.loads(export_json(lit_circle()))
        schema_validator.validate(exported)
        assert exported["version"] == SHOW_FORMAT_VERSION
        assert Show.validate_and_load(exported).has_light_data

    @pytest.mark.parametrize("shape", SHAPES)
    @pytest.mark.parametrize("num_drones", [1, 2, 3, 10, 50])
    def test_every_shape_exports(self, schema_validator, shape, num_drones):
        # Regression: circle/helix/spline used to emit yaw == 360.0 (rounded
        # after wrapping), which the schema's [0, 360) range rejects.
        choreo = getattr(generator, f"generate_{shape}")(num_drones, 30.0, 2.0)
        schema_validator.validate(json.loads(export_json(choreo)))

    @pytest.mark.parametrize("deg,expected", [
        (359.96, 0.0), (360.0, 0.0), (720.04, 0.0), (-0.04, 0.0), (359.94, 359.9), (-90.0, 270.0), (45.25, 45.2),
    ])
    def test_wrap_heading(self, deg, expected):
        assert generator._wrap_heading(deg) == expected


class TestExportGate:
    def test_bad_show_never_leaves_the_studio(self):
        choreo = lit_circle()
        choreo["drones"][1]["waypoints"][3]["yaw"] = 9999.0
        with pytest.raises(ValidationError) as exc:
            export_json(choreo)
        assert exc.value.errors()[0]["loc"] == ("drones", 1, "waypoints", 3, "yaw")

    def test_does_not_mutate_the_callers_dict(self):
        choreo = lit_circle()
        export_json(choreo)
        assert "version" not in choreo

    def test_existing_version_and_unknown_keys_are_kept(self):
        choreo = {**lit_circle(), "version": 2, "_duration": 10.0}
        exported = json.loads(export_json(choreo))
        assert exported["version"] == 2
        assert exported["_duration"] == 10.0

    def test_geofence_block_is_accepted(self):
        # Issue #53: exporter derives _geofence from trajectory envelope.
        exported = json.loads(export_json(lit_circle()))
        assert "_geofence" in exported
        assert set(exported["_geofence"].keys()) == {
            "envelope_radius_m",
            "soft_warning_radius_m",
            "hard_geofence_radius_m",
        }
        assert (
            exported["_geofence"]["envelope_radius_m"]
            < exported["_geofence"]["soft_warning_radius_m"]
            < exported["_geofence"]["hard_geofence_radius_m"]
        )

    def test_nan_in_an_unknown_key_is_refused(self):
        # Not a field the schema knows, but it would still be invalid JSON.
        with pytest.raises(ValueError):
            export_json({**lit_circle(), "_duration": float("nan")})


class TestSeparationGate:
    """Issue #141/#153: /api/export refuses a show whose waypoints — as it
    will actually export them — put two drones closer than design_separation_m.

    Unlike #60's jerk exception just above this gate in server.py, minimum
    separation IS enforced here: it's a collision risk, not a choreography-
    quality metric. The repro is two waypoints already rounded to the
    centimetre exactly as tara-studio-web's sequencer.js rounds every waypoint
    on build (round2) and tara_studio.exporter.export_tara rounds again on its
    own int16-centimetre packing — 1.9875 m apart, inside the 2.0 m limit,
    even though the two points this was derived from were 2.00012 m apart
    before rounding (sequencer.test.js pins that derivation on the JS side).
    """

    _TOO_CLOSE = {
        "drones": [
            {"id": 0, "waypoints": [
                {"time": 0.0, "x": -0.76, "y": -0.61, "z": -1.75, "yaw": 0.0},
            ]},
            {"id": 1, "waypoints": [
                {"time": 0.0, "x": -0.01, "y": -0.01, "z": -0.01, "yaw": 0.0},
            ]},
        ]
    }

    _COMFORTABLY_CLEAR = {
        "drones": [
            {"id": 0, "waypoints": [
                {"time": 0.0, "x": -0.76, "y": -0.61, "z": -1.75, "yaw": 0.0},
            ]},
            {"id": 1, "waypoints": [
                {"time": 0.0, "x": 5.0, "y": -0.01, "z": -1.75, "yaw": 0.0},
            ]},
        ]
    }

    def test_collision_errors_itself_flags_the_repro(self):
        # Pins the claim the gate relies on, independent of the endpoint: this
        # specific pair of rounded waypoints is a real violation of the design
        # threshold, not just of the looser runtime floor collision_errors
        # defaults to.
        errors = collision_errors(self._TOO_CLOSE, limits=DEFAULT_LIMITS, separation=DEFAULT_LIMITS.design_separation_m)
        assert errors
        assert "1.99" in errors[0] or "1.98" in errors[0]

    @pytest.mark.parametrize("fmt", ["json", "tara"])
    def test_export_refuses_the_repro(self, client, fmt):
        res = client.post("/api/export", json={"choreo": self._TOO_CLOSE, "format": fmt})
        assert res.status_code == 400
        detail = res.json()["detail"]
        assert "separation" in detail["message"].lower()
        assert detail["errors"]

    @pytest.mark.parametrize("fmt", ["json", "tara"])
    def test_export_still_succeeds_once_comfortably_clear(self, client, fmt):
        res = client.post("/api/export", json={"choreo": self._COMFORTABLY_CLEAR, "format": fmt})
        assert res.status_code == 200


class TestExportEndpoint:
    def test_schema_violation_is_a_400_with_field_paths(self, client):
        choreo = lit_circle()
        choreo["drones"][0]["waypoints"][2]["r"] = 300
        res = client.post("/api/export", json={"choreo": choreo, "format": "json"})
        assert res.status_code == 400
        [err] = res.json()["detail"]["errors"]
        assert err["loc"] == ["drones", 0, "waypoints", 2, "r"]

    def test_valid_show_exports(self, client):
        res = client.post("/api/export", json={"choreo": lit_circle(), "format": "json"})
        assert res.status_code == 200
        assert json.loads(res.json()["content"])["version"] == SHOW_FORMAT_VERSION
