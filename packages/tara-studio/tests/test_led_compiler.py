"""Issue #14: LED pattern engine and binary .tara compiler.

The acceptance criterion asks for a test "verifying that compiled .tara binary
files contain valid headers, matching drone IDs, and exact checksum hashes" -
those are the three TestCompiledTaraBinary classes below. The pattern tests
cover the five lighting modes the same issue specifies.
"""

import base64
import struct
import zlib

import pytest
from fastapi.testclient import TestClient

from tara_studio.exporter import (
    TARA_HEADER_FMT,
    TARA_HEADER_SIZE,
    TARA_MAGIC,
    TARA_RECORD_SIZE,
    TARA_VERSION,
    export_tara,
    parse_tara,
)
from tara_studio.generator import generate_circle
from tara_studio.led_engine import (
    COLOR_CYCLE,
    PATTERNS,
    SOLID,
    SPARKLE,
    STROBE,
    WAVE_PROPAGATION,
    MAX_LED_SAMPLE_RATE_HZ,
    LedKeyframe,
    apply_track_to_choreo,
    color_at,
    evaluate,
    hex_to_rgbw,
    led_sample_rate_hz,
    upsample_choreo,
    upsample_factor,
)
from tara_studio.server import app
from tara_core import interpolate_waypoint


@pytest.fixture
def show():
    """A small real show, lit solid orange - the compiler's normal input."""
    choreo = generate_circle(4, 12.0, 2.0)
    return apply_track_to_choreo(
        choreo, [LedKeyframe(pattern=SOLID, start_time=0.0, duration=999.0, color="#FF8000")]
    )


class TestCompiledTaraBinary:
    """Headers, drone IDs and checksums of a compiled .tara file."""

    def test_header_is_valid(self, show):
        blob = export_tara(show, rate_hz=2.0)
        magic, version, drone_count, frame_count, rate_hz, duration_ms, reserved, checksum = (
            struct.unpack(TARA_HEADER_FMT, blob[:TARA_HEADER_SIZE])
        )

        assert magic == TARA_MAGIC
        assert version == TARA_VERSION
        assert drone_count == len(show["drones"])
        assert frame_count == sum(len(d["waypoints"]) for d in show["drones"])
        assert rate_hz == pytest.approx(2.0)
        assert reserved == 0
        expected_ms = max(
            int(round(wp["time"] * 1000)) for d in show["drones"] for wp in d["waypoints"]
        )
        assert duration_ms == expected_ms
        assert checksum != 0

    def test_file_size_is_exactly_header_index_and_records(self, show):
        blob = export_tara(show)
        n_drones = len(show["drones"])
        n_frames = sum(len(d["waypoints"]) for d in show["drones"])
        assert len(blob) == TARA_HEADER_SIZE + n_drones * 4 + n_frames * TARA_RECORD_SIZE

    def test_drone_ids_match_the_source_show(self, show):
        back = parse_tara(export_tara(show))
        assert [d["id"] for d in back["drones"]] == [d["id"] for d in show["drones"]]
        # Frame counts must line up per drone, not just in aggregate - a
        # mismatched index would still total correctly while misattributing
        # every frame after the first short drone.
        for src, got in zip(show["drones"], back["drones"]):
            assert len(got["waypoints"]) == len(src["waypoints"])

    def test_checksum_is_crc32_over_the_payload(self, show):
        blob = export_tara(show)
        checksum = struct.unpack(TARA_HEADER_FMT, blob[:TARA_HEADER_SIZE])[7]
        assert checksum == zlib.crc32(blob[TARA_HEADER_SIZE:]) & 0xFFFFFFFF

    def test_compilation_is_deterministic(self, show):
        # Byte-identical re-exports are what make a checksum meaningful as a
        # build fingerprint; SPARKLE's hashed noise is the part most at risk.
        sparkled = apply_track_to_choreo(
            generate_circle(4, 12.0, 2.0),
            [LedKeyframe(pattern=SPARKLE, duration=999.0, color="#FFFFFF", density=0.5)],
        )
        assert export_tara(sparkled, 2.0) == export_tara(sparkled, 2.0)

    @pytest.mark.parametrize("corrupt_at", [TARA_HEADER_SIZE, -1])
    def test_corrupted_payload_fails_the_checksum(self, show, corrupt_at):
        blob = bytearray(export_tara(show))
        blob[corrupt_at] ^= 0xFF
        with pytest.raises(ValueError, match="checksum"):
            parse_tara(bytes(blob))

    def test_truncated_file_is_rejected(self, show):
        with pytest.raises(ValueError, match="checksum"):
            parse_tara(export_tara(show)[:-TARA_RECORD_SIZE])

    def test_foreign_file_is_rejected(self):
        with pytest.raises(ValueError, match="magic"):
            parse_tara(b"NOPE" + bytes(TARA_HEADER_SIZE))

    def test_roundtrip_preserves_position_and_colour(self, show):
        back = parse_tara(export_tara(show))
        for src, got in zip(show["drones"], back["drones"]):
            for a, b in zip(src["waypoints"], got["waypoints"]):
                # cm / centidegree quantisation is the only permitted loss.
                assert b["x"] == pytest.approx(a["x"], abs=0.005)
                assert b["y"] == pytest.approx(a["y"], abs=0.005)
                assert b["z"] == pytest.approx(a["z"], abs=0.005)
                assert b["time"] == pytest.approx(a["time"], abs=0.0005)
                # Yaw normalises to 0..360 on the way out (see
                # _wrap_yaw_cdeg), so a generator's closing 360.0 comes back
                # as 0.0 - compare modulo a full turn, as the dedicated yaw
                # roundtrip test below does.
                assert b["yaw"] == pytest.approx(a["yaw"] % 360.0, abs=0.005)
                assert (b["r"], b["g"], b["b"]) == (a["r"], a["g"], a["b"])

    @pytest.mark.parametrize("yaw", [0.0, 90.0, 180.0, 270.0, 351.0, 359.99])
    def test_full_circle_yaw_survives_the_roundtrip(self, yaw):
        # Regression: yaw is stored as int16 centidegrees, so a naive
        # yaw * 100 overflowed above 327.67 deg and wrapped to a heading
        # pointing the opposite way. Generators emit the full 0..360 range.
        choreo = {"drones": [{"id": 0, "waypoints": [
            {"time": 0.0, "x": 0.0, "y": 0.0, "z": -5.0, "yaw": yaw, "r": 0, "g": 0, "b": 0, "w": 0},
        ]}]}
        got = parse_tara(export_tara(choreo))["drones"][0]["waypoints"][0]["yaw"]
        assert got == pytest.approx(yaw % 360.0, abs=0.005)

    def test_out_of_range_coordinate_raises_rather_than_wrapping(self):
        # 400 m -> 40000 cm overflows int16; silently wrapping would put a
        # drone at roughly -252 m instead.
        bad = {"drones": [{"id": 0, "waypoints": [{"time": 0, "x": 400.0, "y": 0, "z": 0, "yaw": 0}]}]}
        with pytest.raises(ValueError, match="int16"):
            export_tara(bad)


class TestHexParsing:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("#FFF", (255, 255, 255, 0)),
            ("#0080FF", (0, 128, 255, 0)),
            ("#0080FF40", (0, 128, 255, 64)),
            ("0080FF", (0, 128, 255, 0)),
        ],
    )
    def test_parses_supported_forms(self, value, expected):
        assert hex_to_rgbw(value) == expected

    @pytest.mark.parametrize("value", ["#GGGGGG", "#12345", ""])
    def test_rejects_malformed(self, value):
        with pytest.raises(ValueError):
            hex_to_rgbw(value)


class TestPatterns:
    def test_solid_is_constant(self):
        kf = LedKeyframe(pattern=SOLID, duration=10, color="#123456")
        assert {evaluate(kf, t) for t in (0, 1.7, 9.99)} == {(18, 52, 86, 0)}

    def test_color_cycle_interpolates_and_loops(self):
        kf = LedKeyframe(pattern=COLOR_CYCLE, duration=4, palette=["#FF0000", "#00FF00"])
        assert evaluate(kf, 0.0)[:3] == (255, 0, 0)
        assert evaluate(kf, 2.0)[:3] == (0, 255, 0)  # halfway = second stop
        assert evaluate(kf, 1.0)[:3] == (128, 128, 0)  # blended
        # Wraps back to the first stop rather than dead-ending on the last.
        assert evaluate(kf, 3.999)[:3] == pytest.approx((255, 0, 0), abs=1)

    @pytest.mark.parametrize("freq", [2.0, 4.0, 8.0])
    def test_strobe_duty_cycle_holds_across_the_spec_range(self, freq):
        kf = LedKeyframe(pattern=STROBE, duration=10, color="#FFFFFF", frequency_hz=freq, duty_cycle=0.25)
        # Sample well off the flash boundary to avoid float edge effects.
        samples = [evaluate(kf, i / 1000.0 + 1e-6)[0] > 0 for i in range(10_000)]
        assert sum(samples) / len(samples) == pytest.approx(0.25, abs=0.02)

    def test_strobe_starts_lit_at_its_own_start_time(self):
        kf = LedKeyframe(pattern=STROBE, start_time=5.0, duration=5, color="#FFFFFF", frequency_hz=4)
        # Phase is local, so the flash begins at 5.0s - not wherever show-zero
        # phase happens to land.
        assert evaluate(kf, 5.0)[0] == 255

    def test_sparkle_lights_roughly_the_requested_fraction(self):
        kf = LedKeyframe(pattern=SPARKLE, duration=10, color="#FFFFFF", density=0.3)
        lit = sum(1 for i in range(500) if evaluate(kf, 1.0, i, 500)[0] > 0)
        assert lit / 500 == pytest.approx(0.3, abs=0.06)

    def test_sparkle_is_reproducible(self):
        kf = LedKeyframe(pattern=SPARKLE, duration=10, color="#FFFFFF", density=0.5)
        a = [evaluate(kf, 2.0, i, 100) for i in range(100)]
        b = [evaluate(kf, 2.0, i, 100) for i in range(100)]
        assert a == b

    def test_wave_propagation_varies_along_its_axis(self):
        kf = LedKeyframe(
            pattern=WAVE_PROPAGATION, duration=10, color="#00FF00",
            axis="z", wave_length=10.0, wave_speed=5.0,
        )
        crest = evaluate(kf, 0.0, 0, 4, {"x": 0, "y": 0, "z": 0.0})[1]
        trough = evaluate(kf, 0.0, 0, 4, {"x": 0, "y": 0, "z": 5.0})[1]
        assert crest == 255 and trough == 0

    def test_wave_propagation_travels_over_time(self):
        kf = LedKeyframe(
            pattern=WAVE_PROPAGATION, duration=10, color="#00FF00",
            axis="z", wave_length=10.0, wave_speed=5.0,
        )
        at_pos = {"x": 0, "y": 0, "z": 5.0}
        # One second at 5 m/s moves the crest exactly onto z=5.
        assert evaluate(kf, 0.0, 0, 4, at_pos)[1] == 0
        assert evaluate(kf, 1.0, 0, 4, at_pos)[1] == 255

    def test_wave_ignores_the_axes_it_is_not_travelling_along(self):
        kf = LedKeyframe(pattern=WAVE_PROPAGATION, duration=10, color="#00FF00", axis="x")
        a = evaluate(kf, 0.0, 0, 4, {"x": 3.0, "y": 0.0, "z": 0.0})
        b = evaluate(kf, 0.0, 0, 4, {"x": 3.0, "y": 99.0, "z": -42.0})
        assert a == b

    def test_unknown_pattern_is_rejected_at_construction(self):
        with pytest.raises(ValueError, match="unknown pattern"):
            LedKeyframe(pattern="DISCO")

    def test_bad_axis_is_rejected_at_construction(self):
        with pytest.raises(ValueError, match="axis"):
            LedKeyframe(pattern=WAVE_PROPAGATION, axis="w")

    def test_every_declared_pattern_is_evaluable(self):
        # Guards against a pattern being added to PATTERNS but never wired into
        # evaluate(), which would raise only once a designer selected it.
        for name in PATTERNS:
            kf = LedKeyframe(pattern=name, duration=5, color="#3366CC", palette=["#FF0000", "#0000FF"])
            assert len(evaluate(kf, 1.0, 0, 4, {"x": 0, "y": 0, "z": 0})) == 4


class TestTrackComposition:
    def test_time_outside_every_keyframe_is_dark(self):
        track = [LedKeyframe(pattern=SOLID, start_time=2.0, duration=2.0, color="#FFFFFF")]
        assert color_at(track, 0.0) == (0, 0, 0, 0)
        assert color_at(track, 5.0) == (0, 0, 0, 0)
        assert color_at(track, 3.0) == (255, 255, 255, 0)

    def test_keyframe_window_is_half_open(self):
        # Abutting keyframes must not both claim the boundary instant.
        track = [
            LedKeyframe(pattern=SOLID, start_time=0.0, duration=2.0, color="#FF0000"),
            LedKeyframe(pattern=SOLID, start_time=2.0, duration=2.0, color="#00FF00"),
        ]
        assert color_at(track, 1.999)[:3] == (255, 0, 0)
        assert color_at(track, 2.0)[:3] == (0, 255, 0)

    def test_later_keyframe_wins_on_overlap(self):
        track = [
            LedKeyframe(pattern=SOLID, start_time=0.0, duration=10.0, color="#FF0000"),
            LedKeyframe(pattern=SOLID, start_time=1.0, duration=1.0, color="#0000FF"),
        ]
        assert color_at(track, 1.5)[:3] == (0, 0, 255)
        assert color_at(track, 5.0)[:3] == (255, 0, 0)

    def test_apply_track_writes_rgbw_onto_every_waypoint(self):
        choreo = generate_circle(3, 10.0, 2.0)
        lit = apply_track_to_choreo(
            choreo, [LedKeyframe(pattern=SOLID, duration=999.0, color="#10203040")]
        )
        for drone in lit["drones"]:
            for wp in drone["waypoints"]:
                assert (wp["r"], wp["g"], wp["b"], wp["w"]) == (16, 32, 48, 64)

    def test_apply_track_leaves_the_source_show_untouched(self):
        choreo = generate_circle(2, 10.0, 2.0)
        apply_track_to_choreo(choreo, [LedKeyframe(pattern=SOLID, duration=999.0)])
        assert "r" not in choreo["drones"][0]["waypoints"][0]

    def test_apply_track_preserves_trajectory_fields(self):
        choreo = generate_circle(2, 10.0, 2.0)
        lit = apply_track_to_choreo(choreo, [LedKeyframe(pattern=SOLID, duration=999.0)])
        for src, got in zip(choreo["drones"], lit["drones"]):
            for a, b in zip(src["waypoints"], got["waypoints"]):
                assert (b["x"], b["y"], b["z"], b["time"]) == (a["x"], a["y"], a["z"], a["time"])


def _export_tara(choreo, led_track, rate_hz):
    """Compiles through the real /api/export endpoint, as the Studio does."""
    resp = TestClient(app).post(
        "/api/export",
        json={"choreo": choreo, "format": "tara", "led_track": led_track, "rate_hz": rate_hz},
    )
    assert resp.status_code == 200, resp.text
    return parse_tara(base64.b64decode(resp.json()["content"]))


def _flashes(waypoints, start, end):
    """Number of dark-to-lit transitions between `start` and `end` seconds."""
    lit = [wp["r"] > 0 for wp in waypoints if start <= wp["time"] < end]
    return sum(1 for a, b in zip(lit, lit[1:]) if not a and b) + (1 if lit and lit[0] else 0)


class TestStrobeSurvivesExport:
    """Step-3 bug 3: a strobe exported as solid light.

    Colours are baked onto waypoints, so the .tara only holds them at waypoint
    times. The Studio exports at 4 Hz by default, and a 4 Hz strobe sampled at
    4 Hz lands every sample on the same phase - all 40 samples in a 10 s cue
    were ON, and the drones glowed solid.
    """

    RATE = 4.0

    @pytest.fixture
    def choreo(self):
        return generate_circle(3, 30.0, self.RATE)

    def _strobe(self, freq, duty=0.5, start=10.0, duration=10.0):
        return [{"pattern": STROBE, "start_time": start, "duration": duration,
                 "color": "#FFFFFF", "frequency_hz": freq, "duty_cycle": duty}]

    def test_the_reported_case_no_longer_exports_solid(self, choreo):
        show = _export_tara(choreo, self._strobe(4.0), self.RATE)
        for drone in show["drones"]:
            window = [wp["r"] > 0 for wp in drone["waypoints"] if 10.0 <= wp["time"] < 20.0]
            assert not all(window), "strobe exported as solid light"
            assert _flashes(drone["waypoints"], 10.0, 20.0) == 40

    @pytest.mark.parametrize("freq", [2.0, 3.5, 4.0, 5.0, 6.5, 8.0])
    @pytest.mark.parametrize("duty", [0.2, 0.5, 0.8])
    def test_every_flash_survives_across_the_spec_range(self, choreo, freq, duty):
        show = _export_tara(choreo, self._strobe(freq, duty), self.RATE)
        for drone in show["drones"]:
            assert _flashes(drone["waypoints"], 10.0, 20.0) == round(freq * 10.0)

    def test_header_rate_is_the_resampled_rate_and_frames_stay_uniform(self, choreo):
        show = _export_tara(choreo, self._strobe(8.0), self.RATE)
        # 8 Hz at duty 0.5 has 62.5 ms phases; two samples each needs 32 Hz.
        assert show["rate_hz"] == 32.0
        times = [wp["time"] for wp in show["drones"][0]["waypoints"]]
        gaps = {round(b - a, 3) for a, b in zip(times, times[1:])}
        assert gaps <= {0.031, 0.032}  # 31.25 ms, rounded to whole ms on disk

    def test_a_track_without_strobes_exports_at_the_show_rate(self, choreo):
        track = [{"pattern": SOLID, "start_time": 0.0, "duration": 999.0, "color": "#FF0000"}]
        show = _export_tara(choreo, track, self.RATE)
        assert show["rate_hz"] == self.RATE
        assert len(show["drones"][0]["waypoints"]) == len(choreo["drones"][0]["waypoints"])

    def test_a_phase_too_short_for_the_rate_ceiling_is_refused(self, choreo):
        # 8 Hz at duty 0.05 flashes for 6 ms: under one 20 ms frame at 50 Hz.
        resp = TestClient(app).post(
            "/api/export",
            json={"choreo": choreo, "format": "tara",
                  "led_track": self._strobe(8.0, 0.05), "rate_hz": self.RATE},
        )
        assert resp.status_code == 400
        assert "too short to export" in resp.json()["detail"]


class TestUpsampling:
    def test_required_rate_gives_each_phase_two_samples(self):
        kf = LedKeyframe(pattern=STROBE, duration=5, frequency_hz=4.0, duty_cycle=0.25)
        # Shortest phase is the 62.5 ms flash.
        assert led_sample_rate_hz([kf]) == pytest.approx(32.0)

    def test_required_rate_is_capped_when_one_sample_per_phase_still_fits(self):
        kf = LedKeyframe(pattern=STROBE, duration=5, frequency_hz=8.0, duty_cycle=0.2)
        assert led_sample_rate_hz([kf]) == MAX_LED_SAMPLE_RATE_HZ

    @pytest.mark.parametrize("duty", [0.0, 1.0])
    def test_constant_duty_needs_no_particular_rate(self, duty):
        kf = LedKeyframe(pattern=STROBE, duration=5, frequency_hz=8.0, duty_cycle=duty)
        assert led_sample_rate_hz([kf]) == 0.0

    def test_factor_is_one_when_the_show_is_already_fine_enough(self):
        choreo = generate_circle(2, 10.0, 10.0)
        assert upsample_factor(choreo, 8.0) == 1
        assert upsample_factor(choreo, 0.0) == 1

    def test_originals_are_kept_and_new_frames_lie_on_the_flown_path(self):
        choreo = generate_circle(2, 10.0, 2.0)
        dense = upsample_choreo(choreo, 4)
        for src, got in zip(choreo["drones"], dense["drones"]):
            wps = src["waypoints"]
            assert len(got["waypoints"]) == (len(wps) - 1) * 4 + 1
            assert got["waypoints"][::4] == wps
            for wp in got["waypoints"]:
                flown = interpolate_waypoint(wps, wp["time"])
                assert (wp["x"], wp["y"], wp["z"]) == pytest.approx(
                    (flown["x"], flown["y"], flown["z"])
                )
                # 360 and 0 are the same heading.
                d = (wp["yaw"] - flown["yaw"]) % 360.0
                assert min(d, 360.0 - d) == pytest.approx(0.0, abs=1e-9)

    def test_upsampling_leaves_the_source_show_untouched(self):
        choreo = generate_circle(2, 10.0, 2.0)
        before = len(choreo["drones"][0]["waypoints"])
        upsample_choreo(choreo, 3)
        assert len(choreo["drones"][0]["waypoints"]) == before
