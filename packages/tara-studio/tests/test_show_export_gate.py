"""The Studio's JSON export refuses shows that break the show schema (issue #55)."""

import json

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from tara_core import SHOW_FORMAT_VERSION, SHOW_SCHEMA_PATH, Show
from tara_core.geometry import quintic_segment
from tara_studio import generator
from tara_studio.exporter import export_json
from tara_studio.led_engine import apply_track_to_choreo
from tara_studio.server import app

SHAPES = ["circle", "helix", "heart", "cube", "spline"]


@pytest.fixture(scope="module")
def schema_validator():
    return Draft202012Validator(json.loads(SHOW_SCHEMA_PATH.read_text()))


def lit_circle() -> dict:
    """The real Studio pipeline: generate, then bake an LED track onto it."""
    choreo = generator.generate_circle(2, 10.0, 1.0)
    return apply_track_to_choreo(
        choreo, [{"pattern": "SOLID", "start_time": 0, "duration": 999, "color": "#FF8000"}]
    )


def _quintic_smoothed_choreo() -> dict:
    """A densely-sampled, jerk-continuous single-drone trajectory."""
    p0, p1 = (0.0, 0.0, -2.0), (5.0, 0.0, -2.0)
    half = 6.0
    dt = 1.0 / 20.0
    waypoints = []
    t = 0.0
    while t <= 2 * half + 1e-9:
        state = quintic_segment(p0, p1, half, t) if t <= half else quintic_segment(p1, p0, half, t - half)
        x, y, z = state["position"]
        waypoints.append({"time": round(t, 4), "x": x, "y": y, "z": z, "yaw": 0.0})
        t += dt
    return {"drones": [{"id": 0, "waypoints": waypoints}]}


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


class TestExportEndpoint:
    @pytest.fixture(scope="class")
    def client(self):
        return TestClient(app)

    def test_schema_violation_is_a_400_with_field_paths(self, client):
        choreo = _quintic_smoothed_choreo()
        choreo["drones"][0]["waypoints"][2]["r"] = 300
        res = client.post("/api/export", json={"choreo": choreo, "format": "json"})
        assert res.status_code == 400
        [err] = res.json()["detail"]["errors"]
        assert err["loc"] == ["drones", 0, "waypoints", 2, "r"]

    def test_valid_certified_show_exports(self, client):
        res = client.post("/api/export", json={"choreo": _quintic_smoothed_choreo(), "format": "json"})
        assert res.status_code == 200
        assert json.loads(res.json()["content"])["version"] == SHOW_FORMAT_VERSION

    def test_core_passed_but_jerk_failed_blocks_flight_export(self, client):
        # lit_circle() passes core safety (proximity, velocity, accel, yaw),
        # but fails jerk feasibility (44 jerk violations). Flight export MUST fail closed (412).
        res = client.post("/api/export", json={"choreo": lit_circle(), "format": "json"})
        assert res.status_code == 412
        detail = res.json()["detail"]
        assert "Flight export blocked" in str(detail)
        assert "jerk violation" in str(detail).lower()

    def test_core_passed_but_jerk_failed_succeeds_in_preview_mode(self, client):
        # Explicit preview_only mode allows non-flight export with clear labelling.
        res = client.post("/api/export", json={"choreo": lit_circle(), "format": "json", "preview_only": True})
        assert res.status_code == 200
        body = res.json()
        assert body["preview_only"] is True
        assert body["filename"].startswith("PREVIEW_ONLY_")

    def test_unsafe_show_fails_closed_with_412_across_formats(self, client):
        # Create an unsafe colliding show: two drones sitting on the exact same waypoints (0m distance)
        unsafe_choreo = lit_circle()
        # Overwrite drone 1 waypoints to match drone 0 exactly
        unsafe_choreo["drones"][1]["waypoints"] = [dict(wp) for wp in unsafe_choreo["drones"][0]["waypoints"]]

        for fmt in ["json", "csv", "kml", "tara"]:
            res = client.post("/api/export", json={"choreo": unsafe_choreo, "format": fmt})
            assert res.status_code == 412, f"Expected 412 for unsafe format {fmt}, got {res.status_code}"
            body = res.json()
            assert "Flight export blocked" in str(body["detail"])

    def test_preview_only_still_blocks_an_unsafe_colliding_show(self, client):
        # issue #143: preview_only must not be a way to export a genuinely
        # unsafe show. Core safety (here, 0m separation) is never waived.
        unsafe_choreo = lit_circle()
        unsafe_choreo["drones"][1]["waypoints"] = [dict(wp) for wp in unsafe_choreo["drones"][0]["waypoints"]]

        for fmt in ["json", "csv", "kml", "tara"]:
            res = client.post("/api/export", json={"choreo": unsafe_choreo, "format": fmt, "preview_only": True})
            assert res.status_code == 412, f"Expected 412 for unsafe preview {fmt}, got {res.status_code}"
            body = res.json()
            assert "core safety" in str(body["detail"]).lower()

    def test_preview_only_cannot_be_signed(self, client):
        unsafe_choreo = lit_circle()
        res = client.post("/api/export", json={"choreo": unsafe_choreo, "format": "json", "preview_only": True, "sign": True})
        assert res.status_code == 400
        assert "Preview-only exports cannot be signed" in res.json()["detail"]

    def test_malformed_validator_payload_fails_closed_with_412(self, client):
        # A payload that crashes altitude_errors/validate_show.
        res = client.post("/api/export", json={"choreo": {"drones": "not-a-list"}, "format": "json"})
        assert res.status_code == 412
        assert "Export blocked" in res.json()["detail"]

    def test_malformed_validator_payload_fails_closed_in_preview_too(self, client):
        # The same malformed show must not slip through via preview_only.
        res = client.post("/api/export", json={"choreo": {"drones": "not-a-list"}, "format": "json", "preview_only": True})
        assert res.status_code == 412
        assert "Export blocked" in res.json()["detail"]
