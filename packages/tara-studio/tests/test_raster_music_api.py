"""Integration tests for Studio HTTP endpoints: raster formation and music analysis (#90, #91)."""

import base64
import io
import math
import struct
import wave
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from tara_studio.server import app

client = TestClient(app)


def _make_png_circle(size: int = 128, radius: int = 40) -> bytes:
    """Generate a clean RGBA PNG with an opaque circle on a transparent background."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    center = size // 2
    draw.ellipse(
        [center - radius, center - radius, center + radius, center + radius],
        fill=(255, 255, 255, 255),
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_synthetic_wav(duration_s: float = 3.0, sample_rate: int = 22050, bpm: float = 120.0) -> bytes:
    """Generate a valid mono 16-bit PCM WAV with synthetic clicks at the requested BPM."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        total_frames = int(duration_s * sample_rate)
        beat_interval = int(sample_rate * 60.0 / bpm)
        samples = []
        for i in range(total_frames):
            # Click pulse of 100 samples every beat
            if (i % beat_interval) < 100:
                val = int(16000 * math.sin(2 * math.pi * 440 * (i % beat_interval) / sample_rate))
            else:
                val = 0
            samples.append(val)
        raw = struct.pack(f"<{len(samples)}h", *samples)
        w.writeframes(raw)
    return buf.getvalue()


class TestRasterFormationEndpoint:
    def test_valid_raster_image_returns_formation(self):
        png_bytes = _make_png_circle()
        b64 = base64.b64encode(png_bytes).decode("ascii")

        response = client.post(
            "/api/raster-formation",
            json={
                "image_b64": b64,
                "n_drones": 12,
                "scale": 25.0,
                "center_x": 0.0,
                "altitude": 20.0,
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["status"] == "success"
        assert data["source_type"] == "raster"
        assert data["drone_count"] == 12
        assert len(data["coordinates"]) == 12
        for pt in data["coordinates"]:
            assert set(pt.keys()) == {"id", "x", "y", "z"}
        assert "bounding_box" in data

    def test_invalid_base64_rejected(self):
        response = client.post(
            "/api/raster-formation",
            json={
                "image_b64": "!!!not_valid_base64???",
                "n_drones": 10,
            },
        )
        assert response.status_code == 400

    def test_corrupt_image_bytes_rejected(self):
        b64 = base64.b64encode(b"not an actual image file").decode("ascii")
        response = client.post(
            "/api/raster-formation",
            json={
                "image_b64": b64,
                "n_drones": 10,
            },
        )
        assert response.status_code == 400
        assert "unsupported image format" in response.json()["detail"].lower()


class TestMusicAnalysisEndpoint:
    def test_valid_wav_analysis_succeeds(self):
        wav_bytes = _make_synthetic_wav(duration_s=3.0, bpm=120.0)
        b64 = base64.b64encode(wav_bytes).decode("ascii")

        response = client.post(
            "/api/music/analyze",
            json={
                "wav_b64": b64,
                "min_bpm": 60.0,
                "max_bpm": 200.0,
                "beats_per_bar": 4,
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["analyzer_version"] == "1"
        assert "bpm" in data
        assert isinstance(data["beats"], list)
        assert isinstance(data["onsets"], list)
        assert isinstance(data["bars"], dict)
        assert isinstance(data["bars"]["times"], list)
        assert data["duration_s"] == pytest.approx(3.0, abs=0.1)

    def test_silent_audio_rejected_with_specific_code(self):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(22050)
            w.writeframes(b"\x00\x00" * int(22050 * 3.0))
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        response = client.post(
            "/api/music/analyze",
            json={"wav_b64": b64},
        )
        assert response.status_code == 400
        assert "silent" in response.json()["detail"].lower()

    def test_invalid_base64_rejected(self):
        response = client.post(
            "/api/music/analyze",
            json={"wav_b64": "invalid-base-64"},
        )
        assert response.status_code == 400
