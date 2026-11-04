"""Upload hardening for POST /api/raster-formation and POST /api/music/analyze (#90, #91).

Both endpoints take untrusted, base64-encoded files. The core modules already refuse
oversize, bomb-sized, mis-signed and truncated input; these tests pin the gaps an
audit of the HTTP layer found on top of that, and the properties that held up:

  * the request-size limit is enforced before the body is decoded, and before it is
    read at all when the client declares a length;
  * work and memory on an allowed-size image is bounded (a few-KB checkerboard PNG
    used to cost tens of seconds of CPU);
  * ordinary inputs never become an HTTP 500: large indexed / 1-bit / 16-bit PNGs,
    extreme numeric parameters, malformed WAV chunk sizes, a JSON ``NaN`` literal;
  * error bodies do not leak interpreter object addresses;
  * (guards) nothing is written to disk, and CORS matches the other Studio routes.
"""

import base64
import collections
import io
import json
import os
import random
import struct
import sys
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from tara_studio import music_analysis, raster_formation, server, voxelizer
from tara_studio.music_analysis import MusicAnalysisError, analyze_audio

client = TestClient(server.app, raise_server_exceptions=False)

RASTER = "/api/raster-formation"
MUSIC = "/api/music/analyze"


# --------------------------------------------------------------------- helpers


def _png(img: Image.Image, **save) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", **save)
    return buf.getvalue()


def _disc(size: int) -> np.ndarray:
    yy, xx = np.mgrid[:size, :size]
    return np.where((yy - size // 2) ** 2 + (xx - size // 2) ** 2 <= (size // 3) ** 2, 255, 0).astype(np.uint8)


def _raster(png: bytes, **kw):
    return client.post(RASTER, json={"image_b64": base64.b64encode(png).decode(), "n_drones": 12, **kw})


def _wav(seconds: float = 4.0, rate: int = 22050) -> bytes:
    x = np.random.default_rng(0).normal(0, 300, int(seconds * rate))
    x[:: rate // 2] += 20000
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.clip(x, -32768, 32767).astype("<i2").tobytes())
    return buf.getvalue()


@pytest.fixture
def decode_spy(monkeypatch):
    """Records the length of every string handed to base64.b64decode."""
    seen: list[int] = []
    real = base64.b64decode

    def spy(s, *a, **k):
        seen.append(len(s))
        return real(s, *a, **k)

    monkeypatch.setattr(base64, "b64decode", spy)
    return seen


# ------------------------------------------------ 1. size limit before decoding


def test_oversize_raster_body_is_refused_before_it_is_base64_decoded(decode_spy):
    over = base64.b64encode(b"\0" * (raster_formation.MAX_IMAGE_BYTES + 1024 * 1024))  # ~6 MiB raw -> 8 MiB text
    body = b'{"n_drones": 12, "image_b64": "' + over + b'"}'
    r = client.post(RASTER, content=body, headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert not [n for n in decode_spy if n > 1024 * 1024], "the oversize payload was base64-decoded before being refused"


def test_oversize_body_without_a_content_length_is_refused_too(decode_spy):
    over = base64.b64encode(b"\0" * (raster_formation.MAX_IMAGE_BYTES + 1024 * 1024))

    def chunks():  # chunked transfer: no Content-Length for the server to check up front
        yield b'{"n_drones": 12, "image_b64": "'
        for i in range(0, len(over), 65536):
            yield over[i : i + 65536]
        yield b'"}'

    r = client.post(RASTER, content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert not [n for n in decode_spy if n > 1024 * 1024]


def test_oversize_music_body_is_refused_before_it_is_base64_decoded(decode_spy, monkeypatch):
    monkeypatch.setattr(music_analysis, "MAX_BYTES", 10_000)  # the limit is read when the request arrives
    body = json.dumps({"wav_b64": base64.b64encode(b"\0" * 30_000).decode()}).encode()
    r = client.post(MUSIC, content=body, headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert not [n for n in decode_spy if n > 10_000]


def test_a_body_within_the_limit_still_gets_through():
    assert _raster(_png(Image.fromarray(_disc(200), "L"))).status_code == 200
    assert client.post(MUSIC, json={"wav_b64": base64.b64encode(_wav()).decode()}).status_code == 200


# ------------------------------------------------------ 2. bounded CPU work


def test_a_noise_image_with_huge_numbers_of_contours_does_bounded_work(monkeypatch):
    """A 128 px checkerboard is a few hundred bytes of PNG and ~8,000 one-pixel contours
    (a 1024 px one is 500,000+ and cost ~47 s of CPU). Per-contour work must be bounded."""
    calls = collections.Counter()
    for mod in (raster_formation, voxelizer):
        real = mod._contour_length

        def counted(c, _real=real):
            calls["n"] += 1
            return _real(c)

        monkeypatch.setattr(mod, "_contour_length", counted)
    checker = (((np.indices((128, 128)).sum(0)) % 2) * 255).astype(np.uint8)
    r = _raster(_png(Image.fromarray(checker, "L")), n_drones=25)
    assert r.status_code in (200, 400)
    assert calls["n"] < 10_000, f"{calls['n']} contour-length evaluations for one small image"


# ----------------------------------------------- 3. ordinary inputs, never a 500


@pytest.mark.parametrize("mode", ["1", "P", "I;16", "I"])
def test_a_large_image_in_any_png_mode_is_not_a_server_error(mode):
    """Anything over 1024 px is downsampled first, and Image.reduce() raises for these modes."""
    img = Image.fromarray(_disc(1100), "L").convert(mode)
    r = _raster(_png(img))
    assert r.status_code != 500, r.text
    assert r.status_code == 200, r.text


def test_a_large_palette_png_with_transparency_is_not_a_server_error():
    img = Image.fromarray(_disc(1100), "L").convert("P")
    r = _raster(_png(img, transparency=0))
    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    "extra",
    [{"scale": 1e-30}, {"scale": 5e-324}, {"min_separation_m": 1e300}, {"site_radius_m": 1e-300}],
    ids=["scale-1e-30", "scale-denormal", "min-sep-1e300", "site-radius-1e-300"],
)
def test_extreme_numeric_parameters_are_a_client_error_not_a_server_error(extra):
    r = _raster(_png(Image.fromarray(_disc(300), "L")), **extra)
    assert r.status_code != 500, r.text
    assert r.status_code in (200, 400)


def test_a_tiny_scale_does_not_make_the_scale_search_spin(monkeypatch):
    builds = collections.Counter()
    real = raster_formation._build_at_scale

    def counted(*a, **k):
        builds["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(raster_formation, "_build_at_scale", counted)
    _raster(_png(Image.fromarray(_disc(300), "L")), scale=1e-300)
    assert builds["n"] <= 80, f"{builds['n']} scale-growth attempts"


def _wav_with(offset: int, fmt: str, value: int) -> bytes:
    b = bytearray(_wav())
    struct.pack_into(fmt, b, offset, value)
    return bytes(b)


def test_an_oversize_wav_fmt_chunk_is_a_400_not_a_500():
    r = client.post(MUSIC, json={"wav_b64": base64.b64encode(_wav_with(16, "<I", 0xFFFFFFFF)).decode()})
    assert r.status_code == 400, r.text
    assert r.json()["detail"].startswith("corrupt:")


def test_no_corrupted_wav_header_escapes_as_an_unhandled_exception():
    good = _wav()[:20000]
    rng = random.Random(7)
    unhandled = collections.Counter()
    for _ in range(600):
        b = bytearray(good)
        for _ in range(rng.randint(1, 4)):
            b[rng.randrange(0, 64)] = rng.randrange(256)
        try:
            analyze_audio(bytes(b))
        except MusicAnalysisError:
            pass
        except BaseException as e:  # noqa: BLE001 - this is exactly what is being counted
            unhandled[type(e).__name__] += 1
    assert not unhandled, dict(unhandled)


@pytest.mark.parametrize(
    "path,body",
    [
        (MUSIC, b'{"wav_b64": "AAAA", "min_bpm": NaN}'),
        (RASTER, b'{"image_b64": "AA", "n_drones": 3, "scale": NaN}'),
        (RASTER, b'{"image_b64": "AA", "n_drones": 3, "scale": Infinity}'),
    ],
)
def test_a_json_nan_literal_is_a_422_not_a_500(path, body):
    r = client.post(path, content=body, headers={"content-type": "application/json"})
    assert r.status_code == 422, r.text


def test_a_422_does_not_echo_the_submitted_input_back():
    big = ["x"] * 200_000
    r = client.post(RASTER, json={"image_b64": big, "n_drones": 5})
    assert r.status_code == 422
    assert len(r.text) < 2_000, f"{len(r.text)} bytes of response to a bad request"


# ------------------------------------------------------- 4. error-body leaks


@pytest.mark.parametrize(
    "data",
    [
        b"\x89PNG\r\n\x1a\n",
        b"\xff\xd8\xff",
        b"RIFF\x04\x00\x00\x00WEBP",
        b"\xff\xd8\xff" + b"garbage" * 50,
        b"RIFF\x20\x00\x00\x00WEBPgarbagegarbagegarbage",
    ],
    ids=["png-sig", "jpeg-soi", "webp-hdr", "jpeg-garbage", "webp-garbage"],
)
def test_decode_errors_do_not_leak_interpreter_object_addresses(data):
    r = _raster(data)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "0x" not in detail and "BytesIO" not in detail and "object at" not in detail, detail


def test_error_bodies_name_no_paths_or_tracebacks():
    cases = [
        _raster(b"\x89PNG\r\n\x1a\n" + b"x" * 40),
        _raster(b"GIF89a" + b"\0" * 40),
        client.post(RASTER, json={"image_b64": "!!!", "n_drones": 5}),
        client.post(MUSIC, json={"wav_b64": "AAAA"}),
        client.post(MUSIC, json={"wav_b64": base64.b64encode(_wav()[:5000]).decode()}),
    ]
    for r in cases:
        assert r.status_code == 400
        for needle in ("/home", "/usr", "site-packages", "Traceback", ".py", "File \""):
            assert needle not in r.text, (needle, r.text)


# ------------------------------------------------ 5. guards (hold on main too)


_disk_writes: list[str] | None = None  # None = not recording; sys.addaudithook hooks cannot be removed


def _disk_write_hook(event, args):
    if _disk_writes is None:
        return
    if event == "open":
        path, mode, flags = args
        if (isinstance(mode, str) and any(m in mode for m in "wax+")) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)
        ):
            _disk_writes.append(str(path))
    elif event in ("os.mkdir", "os.rename", "os.remove", "tempfile.mkstemp", "tempfile.mkdtemp"):
        _disk_writes.append(event)


sys.addaudithook(_disk_write_hook)


def test_requests_write_nothing_to_disk():
    global _disk_writes
    _disk_writes = []
    try:
        _raster(_png(Image.fromarray(_disc(300), "L")))
        client.post(MUSIC, json={"wav_b64": base64.b64encode(_wav()).decode()})
        _raster(b"\x89PNG\r\n\x1a\n")  # error paths too
        client.post(MUSIC, json={"wav_b64": "AAAA"})
        writes = list(_disk_writes)
    finally:
        _disk_writes = None
    assert not writes, writes


@pytest.mark.parametrize("path", [RASTER, MUSIC, "/api/generate"])
def test_cors_on_the_new_endpoints_matches_the_existing_ones(path):
    origin = server.allowed_origins[0]
    ok = client.options(path, headers={"Origin": origin, "Access-Control-Request-Method": "POST"})
    bad = client.options(path, headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == origin
    assert bad.status_code == 400 and "access-control-allow-origin" not in bad.headers
