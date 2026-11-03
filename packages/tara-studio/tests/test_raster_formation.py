"""Tests for raster image -> formation geometry (issue #90, first slice).

Images are generated in code with Pillow, so there are no binary fixtures.
"""

import io

import numpy as np
import pytest
from PIL import Image, ImageDraw

from tara_core import DEFAULT_LIMITS
from tara_studio import raster_formation as rf
from tara_studio.raster_formation import RasterFormationError, raster_to_formation


def _encode(img: Image.Image, fmt: str = "PNG", **kwargs) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kwargs)
    return buf.getvalue()


def _star_polygon(cx, cy, r_out, r_in, points=5):
    pts = []
    for i in range(points * 2):
        ang = -np.pi / 2 + i * np.pi / points
        r = r_out if i % 2 == 0 else r_in
        pts.append((cx + r * np.cos(ang), cy + r * np.sin(ang)))
    return pts


def opaque_star() -> bytes:
    """Black star on a white opaque background."""
    img = Image.new("RGB", (400, 400), "white")
    ImageDraw.Draw(img).polygon(_star_polygon(200, 205, 180, 75), fill="black")
    return _encode(img)


def transparent_star() -> bytes:
    """Star on a fully transparent background; the RGB under it is identical
    everywhere, so only the alpha channel can reveal the shape."""
    img = Image.new("RGBA", (400, 400), (255, 255, 255, 0))
    ImageDraw.Draw(img).polygon(_star_polygon(200, 205, 180, 75), fill=(255, 255, 255, 255))
    return _encode(img)


def ring() -> bytes:
    img = Image.new("L", (300, 300), 255)
    d = ImageDraw.Draw(img)
    d.ellipse((20, 20, 280, 280), fill=0)
    d.ellipse((90, 90, 210, 210), fill=255)
    return _encode(img)


# The default centre (15 m up) caps a 100-drone star at ~27 m tall, which cannot
# hold 100 drones at 2 m; a realistic high centre gives the show room to grow.
HIGH_CENTER = (0.0, 45.0)


def _points(result):
    return np.array([[c["x"], c["y"], c["z"]] for c in result["coordinates"]])


def _min_sep(pts):
    d = np.linalg.norm(pts[:, None, :] - pts[None, :, :], axis=2)
    np.fill_diagonal(d, np.inf)
    return float(d.min())


class TestDeterminism:
    def test_same_input_gives_identical_output(self):
        data = opaque_star()
        a = raster_to_formation(data, 40)
        b = raster_to_formation(data, 40)
        assert a == b

    def test_params_change_output(self):
        data = opaque_star()
        a = raster_to_formation(data, 40, scale=20.0)
        b = raster_to_formation(data, 40, scale=24.0)
        assert a["coordinates"] != b["coordinates"]


class TestFleetSizes:
    @pytest.mark.parametrize("n", [1, 10, 50, 100])
    def test_returns_exactly_n_points_in_voxelize_shape(self, n):
        res = raster_to_formation(opaque_star(), n, center=HIGH_CENTER)
        assert res["drone_count"] == n
        assert len(res["coordinates"]) == n
        assert [c["id"] for c in res["coordinates"]] == list(range(n))
        assert set(res["coordinates"][0]) == {"id", "x", "y", "z"}
        assert res["source_type"] == "raster"
        assert res["outline_points"] + res["inner_points_added"] == n

    def test_small_fleet_stays_on_outline(self):
        res = raster_to_formation(opaque_star(), 10)
        assert res["inner_points_added"] == 0

    def test_large_fleet_adds_reported_inner_points(self):
        res = raster_to_formation(opaque_star(), 100, scale=10.0, center=HIGH_CENTER)
        assert res["inner_points_added"] > 0
        assert res["outline_points"] + res["inner_points_added"] == 100

    def test_n_out_of_range_rejected(self):
        for bad in (0, -1, 101, True, 2.5):
            with pytest.raises(RasterFormationError):
                raster_to_formation(opaque_star(), bad)


class TestMask:
    def test_transparent_png_uses_alpha(self):
        res = raster_to_formation(transparent_star(), 30)
        assert res["mask_source"] == "alpha"
        # A 5-point star is wider than tall by ~1.05; a filled square mask
        # (what you'd get if alpha were ignored) would be 1:1.
        box = res["bounding_box"]
        width = box["max_x"] - box["min_x"]
        height = box["max_z"] - box["min_z"]
        assert width != pytest.approx(height, rel=0.02)

    def test_opaque_uses_threshold(self):
        res = raster_to_formation(opaque_star(), 30)
        assert res["mask_source"] == "threshold"

    def test_light_on_dark_matches_dark_on_light(self):
        dark_on_light = opaque_star()
        img = Image.open(io.BytesIO(dark_on_light)).convert("L")
        inverted = _encode(Image.eval(img, lambda v: 255 - v))
        a = raster_to_formation(dark_on_light, 30)
        b = raster_to_formation(inverted, 30)
        assert a["bounding_box"] == pytest.approx(b["bounding_box"], abs=0.2)

    def test_fixed_threshold_is_used_and_reported(self):
        res = raster_to_formation(opaque_star(), 20, threshold=100)
        assert res["threshold"] == 100

    def test_jpeg_and_webp_accepted(self):
        img = Image.open(io.BytesIO(opaque_star())).convert("RGB")
        for fmt in ("JPEG", "WEBP"):
            res = raster_to_formation(_encode(img, fmt), 20)
            assert res["drone_count"] == 20

    def test_ring_outlines_both_boundaries(self):
        res = raster_to_formation(ring(), 60)
        pts = _points(res)
        centre = np.array([pts[:, 0].mean(), pts[:, 2].mean()])
        radii = np.hypot(pts[:, 0] - centre[0], pts[:, 2] - centre[1])
        # Inner and outer circle both carry drones: two clearly separate radii.
        assert (radii < radii.max() * 0.6).sum() > 5
        assert (radii > radii.max() * 0.9).sum() > 5


class TestRejection:
    def test_bad_magic_bytes(self):
        with pytest.raises(RasterFormationError, match="unsupported image format"):
            raster_to_formation(b"GIF89a" + b"\x00" * 100, 10)

    def test_real_png_with_corrupted_signature(self):
        data = bytearray(opaque_star())
        data[0] = 0x00
        with pytest.raises(RasterFormationError, match="unsupported image format"):
            raster_to_formation(bytes(data), 10)

    def test_non_image_with_png_signature(self):
        with pytest.raises(RasterFormationError, match="could not be decoded"):
            raster_to_formation(b"\x89PNG\r\n\x1a\n" + b"not really a png", 10)

    def test_gif_and_svg_and_bmp_rejected(self):
        for fmt in ("GIF", "BMP"):
            img = Image.new("RGB", (20, 20), "black")
            with pytest.raises(RasterFormationError):
                raster_to_formation(_encode(img, fmt), 10)
        with pytest.raises(RasterFormationError):
            raster_to_formation(b"<svg xmlns='http://www.w3.org/2000/svg'/>", 10)

    def test_empty_bytes(self):
        with pytest.raises(RasterFormationError, match="empty"):
            raster_to_formation(b"", 10)

    def test_oversized_file(self, monkeypatch):
        monkeypatch.setattr(rf, "MAX_IMAGE_BYTES", 1000)
        with pytest.raises(RasterFormationError, match="byte limit"):
            raster_to_formation(opaque_star(), 10)

    def test_oversized_dimensions(self):
        # Tiny file, huge canvas: the decompression-bomb shape.
        img = Image.new("1", (rf.MAX_IMAGE_DIMENSION + 1, 8), 1)
        data = _encode(img)
        assert len(data) < 5000
        with pytest.raises(RasterFormationError, match="per-side limit"):
            raster_to_formation(data, 10)

    def test_oversized_pixel_count(self):
        side = rf.MAX_IMAGE_DIMENSION
        img = Image.new("1", (side, side), 1)
        assert side * side > rf.MAX_IMAGE_PIXELS
        with pytest.raises(RasterFormationError, match="pixel limit"):
            raster_to_formation(_encode(img), 10)

    @pytest.mark.parametrize("colour", ["white", "black"])
    def test_blank_opaque_image(self, colour):
        with pytest.raises(RasterFormationError, match="blank"):
            raster_to_formation(_encode(Image.new("RGB", (100, 100), colour)), 10)

    def test_fully_transparent_image(self):
        img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        with pytest.raises(RasterFormationError, match="blank"):
            raster_to_formation(_encode(img), 10)

    def test_animated_webp_rejected(self):
        frames = [Image.new("RGB", (40, 40), c) for c in ("black", "white")]
        buf = io.BytesIO()
        frames[0].save(buf, format="WEBP", save_all=True, append_images=frames[1:])
        with pytest.raises(RasterFormationError, match="animated"):
            raster_to_formation(buf.getvalue(), 10)


class TestLimits:
    def test_separation_and_site_radius_hold_at_n100(self):
        res = raster_to_formation(opaque_star(), 100, center=HIGH_CENTER)
        pts = _points(res)
        assert _min_sep(pts) >= DEFAULT_LIMITS.design_separation_m
        assert np.hypot(pts[:, 0], pts[:, 1]).max() <= DEFAULT_LIMITS.geofence_radius_m
        assert (-pts[:, 2]).min() >= 2.0
        assert (-pts[:, 2]).max() <= DEFAULT_LIMITS.max_altitude_m
        assert res["min_separation_m"] >= DEFAULT_LIMITS.design_separation_m

    def test_oversized_scale_is_clamped_to_site_and_reported(self):
        res = raster_to_formation(opaque_star(), 100, scale=500.0, center=HIGH_CENTER)
        assert res["scale_meters"] < res["requested_scale_meters"] == 500.0
        pts = _points(res)
        assert np.hypot(pts[:, 0], pts[:, 1]).max() <= DEFAULT_LIMITS.geofence_radius_m

    def test_cannot_fit_is_a_clear_failure_not_a_squeeze(self):
        # 100 drones at 2 m inside a 4 m radius site cannot happen.
        with pytest.raises(RasterFormationError, match="too small|cannot fit"):
            raster_to_formation(opaque_star(), 100, site_radius_m=4.0)

    def test_limits_cannot_be_loosened(self):
        with pytest.raises(RasterFormationError, match="design separation"):
            raster_to_formation(opaque_star(), 10, min_separation_m=0.5)
        with pytest.raises(RasterFormationError, match="site_radius_m"):
            raster_to_formation(opaque_star(), 10, site_radius_m=DEFAULT_LIMITS.geofence_radius_m + 1)

    def test_stricter_separation_is_honoured(self):
        res = raster_to_formation(opaque_star(), 30, min_separation_m=3.0)
        assert res["min_separation_m"] >= 3.0

    def test_center_with_no_room_fails(self):
        with pytest.raises(RasterFormationError, match="no room"):
            raster_to_formation(opaque_star(), 10, center=(0.0, 1.0))
        with pytest.raises(RasterFormationError, match="no room"):
            raster_to_formation(opaque_star(), 10, center=(40.0, 15.0))

    def test_center_offsets_the_formation(self):
        res = raster_to_formation(opaque_star(), 20, scale=10.0, center=(10.0, 30.0))
        box = res["bounding_box"]
        assert (box["min_x"] + box["max_x"]) / 2 == pytest.approx(10.0, abs=0.5)
        assert -(box["min_z"] + box["max_z"]) / 2 == pytest.approx(30.0, abs=0.5)


class TestGeometryOnly:
    def test_emits_points_only(self):
        res = raster_to_formation(opaque_star(), 10)
        assert not {"trajectories", "waypoints", "show", "choreography"} & set(res)


class TestFinalGate:
    """`_verify` is the last line of defence on the rounded output."""

    def test_rejects_close_pair(self):
        pts = np.array([[0.0, 0.0, -20.0], [1.0, 0.0, -20.0]])
        with pytest.raises(RasterFormationError, match="cannot fit"):
            rf._verify(pts, 2, 30.0, 2.0, 120.0)

    def test_rejects_outside_site_radius(self):
        pts = np.array([[0.0, 0.0, -20.0], [31.0, 0.0, -20.0]])
        with pytest.raises(RasterFormationError, match="site radius"):
            rf._verify(pts, 2, 30.0, 2.0, 120.0)

    def test_rejects_underground_and_above_ceiling(self):
        for z in (-1.0, -121.0):
            pts = np.array([[0.0, 0.0, z], [5.0, 0.0, -20.0]])
            with pytest.raises(RasterFormationError, match="altitude"):
                rf._verify(pts, 2, 30.0, 2.0, 120.0)

    def test_rejects_wrong_count(self):
        pts = np.array([[0.0, 0.0, -20.0]])
        with pytest.raises(RasterFormationError):
            rf._verify(pts, 2, 30.0, 2.0, 120.0)
