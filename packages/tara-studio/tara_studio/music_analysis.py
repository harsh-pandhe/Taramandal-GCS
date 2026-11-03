"""Music analysis core for the Studio (issue #91, first slice).

Takes the bytes of an uploaded WAV file and returns a plain, JSON-serialisable
dict describing its rhythm: tempo, beat grid, onsets and a bar-start guess. The
result carries the settings it was produced with and an analyzer version, so it
can be cached with the project and recomputed when either changes.

This is advisory. It never emits trajectories, never touches show data and
never calls the compiler: a designer may snap cues to the grid, nothing here
moves a drone. Section detection (intro / chorus / drop) is not part of this
slice.

The input is an untrusted upload. The format is decided from the RIFF/WAVE
header, not the file name; size and duration are capped before any sample is
decoded; empty, silent, truncated or unsupported files are refused with a
:class:`MusicAnalysisError` whose ``code`` says why. Decoding is the stdlib
``wave`` module (16-bit and 24-bit PCM, mono or stereo) and the analysis is
numpy only.

Algorithm:

  1. Downmix to mono.
  2. Onset-strength envelope: log-compressed spectral flux over Hann-windowed
     FFT frames at a fixed hop, with a moving mean removed.
  3. Tempo: autocorrelation of the envelope within ``[min_bpm, max_bpm]``,
     weighted by a broad preference for tempos near 120 BPM (this settles
     half/double-tempo ties), then refined by fitting a beat grid.
  4. Beats: the grid (period and phase) that best matches the envelope.
  5. Bars: the beat offset (mod ``beats_per_bar``) whose beats carry the most
     onset energy is guessed as the downbeat.

Everything is deterministic: no randomness, fixed hop and window, and times
rounded to milliseconds, so the same audio and settings give an identical dict.
Confidences are reported as measured; low means low.
"""

from __future__ import annotations

import bisect
import io
import math
import wave
from collections.abc import Sequence
from typing import Any

import numpy as np

#: Bumped whenever a change can alter the result for the same audio + settings.
ANALYZER_VERSION = "1"

#: Largest upload accepted, in bytes.
MAX_BYTES = 64 * 1024 * 1024
#: Longest audio accepted, in seconds. Checked from the header, before decoding.
MAX_DURATION_S = 600.0
#: Shortest audio worth analysing: a couple of beats at the slowest tempo.
MIN_DURATION_S = 2.0
MIN_SAMPLE_RATE = 8000
MAX_SAMPLE_RATE = 192000
#: Peak amplitude (full scale = 1.0) at or below which a file counts as silent.
SILENCE_PEAK = 1e-3

DEFAULT_SETTINGS: dict[str, Any] = {
    "min_bpm": 60.0,
    "max_bpm": 200.0,
    "beats_per_bar": 4,
}
_SETTING_KEYS = frozenset(DEFAULT_SETTINGS)

# Fixed analysis frame, in seconds; converted to samples from the file's rate.
_HOP_S = 0.010
_WINDOW_S = 0.023
# Moving-mean length removed from the flux, and onset peak picking.
_MEAN_S = 0.5
_PEAK_RADIUS_FRAMES = 3
_ONSET_MIN_STRENGTH = 0.1
# Fewer onsets than this cannot show a periodicity (a held tone has one, at its start).
_MIN_ONSETS_FOR_TEMPO = 4
# Flux peak (in log-compressed magnitude units) below which the audio is taken to
# have no onsets: a steady tone ripples at ~0.07, the quietest real click is >8.
_MIN_FLUX_PEAK = 1.0
# Log compression applied to FFT magnitude before differencing.
_COMPRESSION = 1000.0
# Tempo prior: log-Gaussian around this BPM, sigma in octaves.
_PRIOR_BPM = 120.0
_PRIOR_SIGMA_OCT = 1.0
# Beat-grid refinement: relative period span searched around the estimate.
_GRID_SPAN = 0.03
_GRID_STEPS = 60
# Envelope frames processed per FFT batch, to bound memory on long files.
_CHUNK_FRAMES = 2048


class MusicAnalysisError(Exception):
    """The audio (or the settings) cannot be analysed.

    ``code`` is a stable machine-readable reason; ``str()`` is for people.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def _decode_wav(data: bytes) -> tuple[np.ndarray, int]:
    """Validate and decode ``data`` to (mono float32 in [-1, 1], sample rate)."""
    if len(data) > MAX_BYTES:
        raise MusicAnalysisError(
            "too_large", f"file is {len(data)} bytes; the limit is {MAX_BYTES}"
        )
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise MusicAnalysisError("not_wav", "not a WAV file (missing RIFF/WAVE header)")

    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            channels = wf.getnchannels()
            width = wf.getsampwidth()
            rate = wf.getframerate()
            frames = wf.getnframes()
            comp = wf.getcomptype()
            if comp != "NONE":
                raise MusicAnalysisError("unsupported", f"compressed WAV ({comp}) is not supported")
            if channels not in (1, 2):
                raise MusicAnalysisError("unsupported", f"{channels} channels; only mono or stereo")
            if width not in (2, 3):
                raise MusicAnalysisError(
                    "unsupported", f"{width * 8}-bit samples; only 16-bit or 24-bit PCM"
                )
            if not MIN_SAMPLE_RATE <= rate <= MAX_SAMPLE_RATE:
                raise MusicAnalysisError("unsupported", f"sample rate {rate} Hz is out of range")
            if frames == 0:
                raise MusicAnalysisError("corrupt", "the file holds no audio")
            duration = frames / rate
            if duration > MAX_DURATION_S:
                raise MusicAnalysisError(
                    "too_long", f"{duration:.0f} s of audio; the limit is {MAX_DURATION_S:.0f} s"
                )
            if duration < MIN_DURATION_S:
                raise MusicAnalysisError(
                    "too_short", f"{duration:.2f} s of audio; need at least {MIN_DURATION_S:.0f} s"
                )
            raw = wf.readframes(frames)
    except MusicAnalysisError:
        raise
    except (wave.Error, EOFError) as exc:
        raise MusicAnalysisError("corrupt", f"unreadable WAV: {exc}") from exc

    if len(raw) != frames * channels * width:
        raise MusicAnalysisError("corrupt", "the audio data is truncated")

    if width == 2:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    else:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        samples = v.astype(np.float32) / 8388608.0
    if channels == 2:
        samples = samples.reshape(-1, 2).mean(axis=1, dtype=np.float32)
    return samples, rate


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _resolve_settings(settings: dict | None) -> dict[str, Any]:
    merged = dict(DEFAULT_SETTINGS)
    if settings:
        unknown = set(settings) - _SETTING_KEYS
        if unknown:
            raise MusicAnalysisError("bad_settings", f"unknown settings: {sorted(unknown)}")
        merged.update(settings)
    try:
        lo, hi = float(merged["min_bpm"]), float(merged["max_bpm"])
        per_bar = merged["beats_per_bar"]
    except (TypeError, ValueError) as exc:
        raise MusicAnalysisError("bad_settings", f"non-numeric setting: {exc}") from exc
    if isinstance(per_bar, bool) or not isinstance(per_bar, int) or not 2 <= per_bar <= 12:
        raise MusicAnalysisError("bad_settings", "beats_per_bar must be an integer from 2 to 12")
    if not (math.isfinite(lo) and math.isfinite(hi) and 30.0 <= lo < hi <= 300.0):
        raise MusicAnalysisError("bad_settings", "need 30 <= min_bpm < max_bpm <= 300")
    if hi / lo < 1.1:
        raise MusicAnalysisError("bad_settings", "max_bpm must be at least 10% above min_bpm")
    return {"min_bpm": lo, "max_bpm": hi, "beats_per_bar": per_bar}


# ---------------------------------------------------------------------------
# Signal analysis
# ---------------------------------------------------------------------------


def _onset_envelope(x: np.ndarray, rate: int) -> tuple[np.ndarray, float, int, int]:
    """Spectral-flux envelope normalised to a peak of 1.

    Returns (envelope, frame period in s, hop, window). Frame ``i`` is centred at
    ``i * period + window / 2 / rate`` seconds. An all-zero envelope means the
    audio has no onsets at all.
    """
    hop = max(1, round(rate * _HOP_S))
    window = 1 << max(6, math.ceil(math.log2(rate * _WINDOW_S)))
    period = hop / rate
    n_frames = max(2, 1 + (len(x) - window) // hop) if len(x) >= window else 2
    padded_len = (n_frames - 1) * hop + window
    if len(x) < padded_len:
        x = np.concatenate([x, np.zeros(padded_len - len(x), dtype=x.dtype)])

    hann = np.hanning(window).astype(np.float32)
    scale = 1.0 / hann.sum()
    flux = np.zeros(n_frames, dtype=np.float64)
    prev: np.ndarray | None = None
    for start in range(0, n_frames, _CHUNK_FRAMES):
        stop = min(n_frames, start + _CHUNK_FRAMES)
        seg = x[start * hop : (stop - 1) * hop + window]
        frames = np.lib.stride_tricks.sliding_window_view(seg, window)[::hop][: stop - start]
        mag = np.abs(np.fft.rfft(frames * hann, axis=1)) * scale
        logmag = np.log1p(_COMPRESSION * mag).astype(np.float64)
        diff = np.diff(logmag, axis=0, prepend=logmag[:1] if prev is None else prev[None, :])
        flux[start:stop] = np.maximum(diff, 0.0).sum(axis=1)
        prev = logmag[-1]

    mean_len = max(3, round(_MEAN_S / period))
    kernel = np.ones(mean_len) / mean_len
    # Divide by the window's coverage so the mean is not dragged down at the edges.
    local_mean = np.convolve(flux, kernel, mode="same") / np.convolve(
        np.ones_like(flux), kernel, mode="same"
    )
    env = np.maximum(flux - local_mean, 0.0)
    peak = env.max()
    if peak < _MIN_FLUX_PEAK:
        return np.zeros_like(env), period, hop, window
    return env / peak, period, hop, window


def _pick_onsets(env: np.ndarray) -> list[int]:
    """Frame indices of envelope peaks, at least ``_PEAK_RADIUS_FRAMES`` apart."""
    r = _PEAK_RADIUS_FRAMES
    padded = np.pad(env, r)
    local_max = np.lib.stride_tricks.sliding_window_view(padded, 2 * r + 1).max(axis=1)
    candidates = np.flatnonzero((env >= local_max) & (env >= _ONSET_MIN_STRENGTH))
    picked: list[int] = []
    for i in candidates:
        if picked and i - picked[-1] <= r:
            if env[i] > env[picked[-1]]:
                picked[-1] = int(i)
            continue
        picked.append(int(i))
    return picked


def _estimate_tempo(env: np.ndarray, period: float, lo: float, hi: float) -> tuple[float, float]:
    """(bpm, confidence) from the envelope autocorrelation; (0, 0) if no lag fits."""
    n = len(env)
    centred = env - env.mean()
    if not np.any(centred):
        return 0.0, 0.0
    size = 1 << (2 * n - 1).bit_length()
    spec = np.fft.rfft(centred, size)
    ac = np.fft.irfft(spec * np.conj(spec), size)[:n]
    if ac[0] <= 0:
        return 0.0, 0.0
    # Unbiased (divide by overlap) and normalised so a perfectly periodic
    # envelope scores 1 at its period.
    ac = ac / ac[0] * (n / (n - np.arange(n)))

    lag_min = max(1, math.floor(60.0 / hi / period))
    lag_max = min(n - 2, math.ceil(60.0 / lo / period))
    if lag_max <= lag_min + 1:
        return 0.0, 0.0
    lags = np.arange(lag_min, lag_max + 1)
    bpms = 60.0 / (lags * period)
    in_range = (bpms >= lo) & (bpms <= hi)
    lags, bpms = lags[in_range], bpms[in_range]
    if len(lags) == 0:
        return 0.0, 0.0
    scores = ac[lags]
    prior = np.exp(-0.5 * (np.log2(bpms / _PRIOR_BPM) / _PRIOR_SIGMA_OCT) ** 2)
    best = int(np.argmax(scores * prior))
    peak = float(scores[best])

    # Parabolic interpolation of the lag for sub-frame tempo resolution.
    lag = float(lags[best])
    k = int(lags[best])
    if 1 <= k < n - 1:
        a, b, c = ac[k - 1], ac[k], ac[k + 1]
        denom = a - 2 * b + c
        if denom < 0:
            lag += float(np.clip(0.5 * (a - c) / denom, -1.0, 1.0))
    bpm = float(np.clip(60.0 / (lag * period), lo, hi))

    baseline = float(np.median(ac[lags]))
    confidence = float(np.clip((peak - baseline) / max(1e-9, 1.0 - baseline), 0.0, 1.0))
    return bpm, confidence


def _fit_beat_grid(env: np.ndarray, frame_offset: float, period: float, bpm: float, lo: float, hi: float):
    """Best (beat period in s, first-beat time in s) near ``bpm``, plus its beats' times."""
    n = len(env)
    smooth = np.convolve(env, [0.25, 0.5, 0.25], mode="same")
    duration_frames = n
    best_score, best_p, best_phase = -1.0, 0.0, 0.0
    base_period = 60.0 / bpm
    for step in range(-_GRID_STEPS, _GRID_STEPS + 1):
        beat_period = base_period * (1.0 + _GRID_SPAN * step / _GRID_STEPS)
        if not lo <= 60.0 / beat_period <= hi:
            continue
        pf = beat_period / period  # beat period in frames
        phases = np.arange(0.0, pf, 0.5)
        n_beats = int(duration_frames // pf) + 1
        pos = phases[:, None] + pf * np.arange(n_beats)[None, :]
        idx = np.rint(pos).astype(int)
        valid = idx < n
        vals = np.where(valid, smooth[np.minimum(idx, n - 1)], 0.0)
        counts = np.maximum(valid.sum(axis=1), 1)
        scores = vals.sum(axis=1) / counts
        j = int(np.argmax(scores))
        if scores[j] > best_score:
            best_score, best_p, best_phase = float(scores[j]), beat_period, float(phases[j]) * period
    return best_p, best_phase + frame_offset


def _round3(v: float) -> float:
    return round(float(v), 3) + 0.0  # + 0.0 turns -0.0 into 0.0


def analyze_audio(wav_bytes: bytes, *, settings: dict | None = None) -> dict:
    """Analyse the WAV file ``wav_bytes`` and return a JSON-serialisable dict.

    ``settings`` may override ``min_bpm`` (default 60), ``max_bpm`` (200) and
    ``beats_per_bar`` (4); the values used are echoed in ``settings_used``.
    Raises :class:`MusicAnalysisError` if the input or settings are refused.

    ``bpm`` is ``None`` (confidence 0, no beats) when the audio has too few onsets
    to time, such as a held tone. Confidences are in [0, 1] and are measurements,
    not guarantees: ``bpm_confidence`` is how periodic the onsets are, a beat's
    ``confidence`` is how much onset energy sits at it, and the bars
    ``confidence`` is how clearly one beat position in the bar is accented.
    """
    used = _resolve_settings(settings)
    x, rate = _decode_wav(bytes(wav_bytes))
    if float(np.max(np.abs(x))) <= SILENCE_PEAK:
        raise MusicAnalysisError("silent", "the audio is silent")

    duration = len(x) / rate
    env, period, hop, window = _onset_envelope(x, rate)
    frame_offset = window / 2 / rate

    onset_frames = _pick_onsets(env) if env.any() else []
    onsets = [
        {"t": _round3(i * period + frame_offset), "strength": _round3(env[i])}
        for i in onset_frames
    ]

    result: dict[str, Any] = {
        "analyzer_version": ANALYZER_VERSION,
        "duration_s": _round3(duration),
        "sample_rate": rate,
        "bpm": None,
        "bpm_confidence": 0.0,
        "beats": [],
        "onsets": onsets,
        "bars": {
            "beats_per_bar": used["beats_per_bar"],
            "downbeat_beat_index": None,
            "confidence": 0.0,
            "times": [],
        },
        "settings_used": used,
    }

    if len(onset_frames) < _MIN_ONSETS_FOR_TEMPO:
        return result

    bpm, bpm_conf = _estimate_tempo(env, period, used["min_bpm"], used["max_bpm"])
    if bpm <= 0.0:
        return result

    beat_period, first = _fit_beat_grid(env, frame_offset, period, bpm, used["min_bpm"], used["max_bpm"])
    if beat_period <= 0.0:
        return result
    bpm = 60.0 / beat_period

    n_beats = int((duration - first) // beat_period) + 1
    times = first + beat_period * np.arange(max(0, n_beats))
    strengths = []
    for t in times:
        c = int(round((t - frame_offset) / period))
        lo_i, hi_i = max(0, c - 1), min(len(env), c + 2)
        strengths.append(float(env[lo_i:hi_i].max()) if hi_i > lo_i else 0.0)

    per_bar = used["beats_per_bar"]
    accents = np.zeros(per_bar)
    for k, s in enumerate(strengths):
        accents[k % per_bar] += s
    order = np.argsort(-accents, kind="stable")
    down = int(order[0])
    top, second = float(accents[order[0]]), float(accents[order[1]])
    bar_conf = (top - second) / top if top > 0 else 0.0
    # With fewer than two full bars there is nothing to compare against.
    if len(times) < 2 * per_bar:
        bar_conf = 0.0

    result["bpm"] = round(float(bpm), 2)
    result["bpm_confidence"] = _round3(bpm_conf)
    result["beats"] = [{"t": _round3(t), "confidence": _round3(s)} for t, s in zip(times, strengths)]
    result["bars"] = {
        "beats_per_bar": per_bar,
        "downbeat_beat_index": down,
        "confidence": _round3(bar_conf),
        "times": [_round3(times[k]) for k in range(down, len(times), per_bar)],
    }
    return result


def snap_to_grid(t: float, marks: Sequence[float], max_shift_s: float | None = None) -> float:
    """The mark nearest to ``t``; ``t`` itself if there are no marks or the
    nearest is more than ``max_shift_s`` away. Ties go to the earlier mark.

    ``marks`` are times in seconds (beat or bar times), in any order. Pure: it
    modifies nothing.
    """
    if not math.isfinite(t):
        raise ValueError("t must be finite")
    if max_shift_s is not None and not max_shift_s >= 0:
        raise ValueError("max_shift_s must be >= 0")
    if not marks:
        return t
    ordered = sorted(marks)
    i = bisect.bisect_left(ordered, t)
    candidates = ordered[max(0, i - 1) : i + 1]
    nearest = min(candidates, key=lambda m: (abs(m - t), m))
    if max_shift_s is not None and abs(nearest - t) > max_shift_s:
        return t
    return nearest
