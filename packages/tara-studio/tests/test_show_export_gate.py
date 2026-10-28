"""The Studio's JSON export refuses shows that break the show schema (issue #55)."""

import json
import os

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from tara_core import DEFAULT_LIMITS, SHOW_FORMAT_VERSION, SHOW_SCHEMA_PATH, Show
from tara_core.geometry import quintic_segment
from tara_core.validate import collision_errors
from tara_studio import generator
from tara_studio.exporter import export_json
from tara_studio.led_engine import apply_track_to_choreo
from tara_studio.server import app
from tara_studio.validator import certify_feasibility_report

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


class TestSeparationGate:
    """Issue #141/#153: /api/export refuses a show whose waypoints — as it
    will actually export them — put two drones closer than design_separation_m.

    The repro is two waypoints already rounded to the centimetre exactly as
    tara-studio-web's sequencer.js rounds every waypoint on build (round2)
    and tara_studio.exporter.export_tara rounds again on its own
    int16-centimetre packing — 1.9875 m apart, inside the 2.0 m limit, even
    though the two points this was derived from were 2.00012 m apart before
    rounding (sequencer.test.js pins that derivation on the JS side).

    Release review on #141 (2026-10-26): an earlier version of this gate ran
    its own collision_errors sweep directly in server.py, on the pre-encode
    choreo. That duplicated, and for .tara was actually weaker than, what
    #143 already added to the canonical gate: certify_feasibility_report's
    own proximity check (which, same as here, operates on choreo before
    format-specific rounding) plus #143/#157's own post-quantization
    .tara-bytes re-check (see TestQuantizationGate below). This fixture
    doesn't need that second, .tara-specific re-check to be caught - the
    input is already at the "already-eroded" 1.9875m value, so the
    canonical gate's own proximity check catches it directly, as a 412
    "Flight export blocked", the same way any other proximity violation is
    - so the duplicate gate was removed from server.py rather than kept
    alongside it. This test asserts that refusal; it would fail if the
    canonical gate's proximity check were ever disabled or weakened.
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
        assert res.status_code == 412
        detail = res.json()["detail"]
        assert "flight export blocked" in str(detail).lower()

    @pytest.mark.parametrize("fmt", ["json", "tara"])
    def test_export_still_succeeds_once_comfortably_clear(self, client, fmt):
        res = client.post("/api/export", json={"choreo": self._COMFORTABLY_CLEAR, "format": fmt})
        assert res.status_code == 200


class TestExportEndpoint:
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

    def test_preview_only_is_not_auto_signed_either(self, client, tmp_path, monkeypatch):
        # Issue #143 review (Vishal): sign is a tri-state (True/False/None);
        # the check above only caught an explicit sign=True. Leaving `sign`
        # unset (the default, and what most callers would actually send) hit
        # sign_export's own auto-sign-when-a-key-exists path untouched by
        # that check, so a preview export WAS signed whenever the server had
        # a key configured - the one thing standing between it and a real
        # upload under the GCS's default policy. No `sign` key sent at all,
        # matching how a typical caller would call this.
        from tara_core.signing import generate_private_key, private_key_to_pem
        from tara_studio import signing as studio_signing

        key_path = tmp_path / "studio.pem"
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(private_key_to_pem(generate_private_key()))
        monkeypatch.setattr(studio_signing, "DEFAULT_TOML", tmp_path / "tara.toml")
        monkeypatch.setenv(studio_signing.SIGNING_KEY_ENV, str(key_path))

        res = client.post("/api/export", json={"choreo": _quintic_smoothed_choreo(), "format": "json", "preview_only": True})
        assert res.status_code == 200
        assert res.json().get("signature") is None

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


class TestQuantizationGate:
    """Issue #157, found by independent review (Vishal): certify_feasibility_report runs on
    `choreo` as given, before export_tara's own int16-centimetre packing - a
    second, independent rounding pass on top of whatever the caller already
    applied. A pair analytically just over 2.0 m can come back under it once
    actually quantized, and the certification step never saw that version.

    Issue #175, found by independent re-review (Vishal): the #157 fix only
    re-checked separation on the rounded bytes. Velocity, acceleration, yaw
    rate and jerk are equally derived from the (now-moved) positions and can
    break the same way - a sweep of 1026 pre-certified shows found 9 that
    exported 200 with decoded bytes failing certification. The gate now
    re-runs the full canonical certification (certify_feasibility_report)
    plus altitude_errors on the decoded bytes, not a separation-only check.
    """

    @pytest.fixture(scope="class")
    def client(self):
        return TestClient(app)

    #: Exact reproduction: true 3D separation 2.000120170725442 m (clears the
    #: design limit, so certify_feasibility_report never objects), but decoding
    #: the real .tara bytes this choreo compiles to measures 1.9874858490062262 m.
    _ERODES_ON_TARA = {
        "drones": [
            {"id": 0, "waypoints": [
                {"time": 0.0, "x": -0.7649384176267587, "y": -0.6145833536300834, "z": -1.7546073069192394, "yaw": 0.0},
            ]},
            {"id": 1, "waypoints": [
                {"time": 0.0, "x": -0.006217378944141877, "y": -0.00870860593605212, "z": -0.0059681580638111555, "yaw": 0.0},
            ]},
        ]
    }

    #: Issue #175's own repro: a single-drone 10 Hz quintic out-and-back
    #: (L=8m, half-time=2.5s, z=5m) certifies pre-rounding (max jerk 18.66
    #: m/s^3) but decodes from the real .tara bytes to 1 velocity + 3 jerk
    #: violations (max jerk exactly 20.0 - the rounding pushed a
    #: comfortably-clear show right to the limit). Confirmed by direct
    #: reproduction against certify_feasibility_report before writing this
    #: test, not copied from the issue text unverified.
    @staticmethod
    def _velocity_jerk_erodes_on_tara():
        p0, p1 = (0.0, 0.0, -5.0), (8.0, 0.0, -5.0)
        half = 2.5
        dt = 1.0 / 10.0
        waypoints = []
        t = 0.0
        while t <= 2 * half + 1e-9:
            state = quintic_segment(p0, p1, half, t) if t <= half else quintic_segment(p1, p0, half, t - half)
            x, y, z = state["position"]
            waypoints.append({"time": round(t, 4), "x": x, "y": y, "z": z, "yaw": 0.0})
            t += dt
        return {"drones": [{"id": 0, "waypoints": waypoints}]}

    def test_tara_export_is_blocked_when_quantization_erodes_separation(self, client):
        res = client.post("/api/export", json={"choreo": self._ERODES_ON_TARA, "format": "tara"})
        assert res.status_code == 412
        detail = res.json()["detail"]
        assert "centimetre rounding" in str(detail).lower()
        assert detail["validation"]["violations"]["proximity"]

    def test_preview_only_tara_export_is_blocked_too(self, client):
        # Core safety (including this) is never waived for preview, issue #143.
        res = client.post("/api/export", json={"choreo": self._ERODES_ON_TARA, "format": "tara", "preview_only": True})
        assert res.status_code == 412
        assert "centimetre rounding" in str(res.json()["detail"]).lower()

    def test_json_export_of_the_same_choreo_is_unaffected(self, client):
        # json/csv/kml apply no further rounding beyond what's in the input,
        # so the pre-encode certification already covers them; this is purely
        # a .tara (int16-centimetre packing) concern.
        res = client.post("/api/export", json={"choreo": self._ERODES_ON_TARA, "format": "json"})
        assert res.status_code == 200

    def test_tara_export_is_blocked_when_quantization_erodes_velocity_or_jerk(self, client):
        # Issue #175's own acceptance: the gap a separation-only re-check
        # missed. certify_feasibility_report must pass on this choreo
        # pre-rounding (otherwise this isn't testing quantization at all,
        # it's testing the pre-rounding gate, which #157/earlier tests
        # already cover) - checked directly, not assumed.
        choreo = self._velocity_jerk_erodes_on_tara()
        pre_certified, _ = certify_feasibility_report(choreo)
        assert pre_certified, "fixture must certify before rounding, to isolate the quantization-only gap"

        res = client.post("/api/export", json={"choreo": choreo, "format": "tara"})
        assert res.status_code == 412
        detail = res.json()["detail"]
        assert "centimetre rounding" in str(detail).lower()
        violations = detail["validation"]["violations"]
        assert violations["jerk"]
        assert violations["velocity"]

    def test_preview_only_tara_export_tolerates_jerk_violations_after_rounding(self, client):
        # The jerk exemption for preview exports (issue #143) applies to the
        # *post-rounding* re-check too, not just the pre-rounding gate - a
        # real generator's default output (0 core-safety violations, ~50
        # jerk violations, same fixture test_export_feasibility_gate.py
        # uses) stays core-safe after quantization too (checked directly
        # before writing this test), isolating the exemption from whether
        # the jerk violations originated before or after rounding.
        choreo = generator.generate_circle(3, 30.0, 2.0)
        res = client.post("/api/export", json={"choreo": choreo, "format": "tara", "preview_only": True})
        assert res.status_code == 200

    def test_a_comfortably_clear_pair_still_exports_as_tara(self, client):
        safe_choreo = {
            "drones": [
                {"id": 0, "waypoints": [{"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0}]},
                {"id": 1, "waypoints": [{"time": 0.0, "x": 5.0, "y": 0.0, "z": -2.0, "yaw": 0.0}]},
            ]
        }
        res = client.post("/api/export", json={"choreo": safe_choreo, "format": "tara"})
        assert res.status_code == 200
