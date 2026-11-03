"""Coverage for the music analysis core (issue #91, first slice).

Every WAV here is synthesised in code, so the true tempo and beat grid are
known exactly and the tests can say how far the analysis is allowed to be off.
"""

import io
import json
import wave

import numpy as np
import pytest

from tara_studio import music_analysis
from tara_studio.music_analysis import (
    ANALYZER_VERSION,
    MusicAnalysisError,
    analyze_audio,
    snap_to_grid,
)

SR = 44100
FIRST_CLICK_S = 0.25


def make_wav(samples, *, sr=SR, bits=16, channels=1) -> bytes:
    """Encode float samples in [-1, 1] as a PCM WAV."""
    x = np.asarray(samples, dtype=np.float64)
    if channels == 2:
        x = np.stack([x, x * 0.5], axis=1).ravel()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(bits // 8)
        wf.setframerate(sr)
        if bits == 16:
            wf.writeframes((x * 32767).astype("<i2").tobytes())
        else:
            v = (x * 8388607).astype("<i4")
            wf.writeframes(np.frombuffer(v.tobytes(), dtype=np.uint8).reshape(-1, 4)[:, :3].tobytes())
    return buf.getvalue()


def click_samples(bpm, *, seconds=12.0, sr=SR, noise=0.0):
    """A decaying 1 kHz blip on every beat, the first at FIRST_CLICK_S."""
    n = int(seconds * sr)
    x = np.zeros(n)
    t_burst = np.arange(int(0.02 * sr)) / sr
    burst = 0.8 * np.sin(2 * np.pi * 1000 * t_burst) * np.exp(-200 * t_burst)
    t = FIRST_CLICK_S
    while t < seconds - 0.03:
        i = round(t * sr)
        x[i : i + len(burst)] += burst
        t += 60.0 / bpm
    if noise:
        x += np.random.default_rng(7).normal(0.0, noise, n)
    return np.clip(x, -1.0, 1.0)


def max_grid_error(beats, bpm):
    """Largest distance of any reported beat from the true click grid."""
    period = 60.0 / bpm
    return max(abs((b["t"] - FIRST_CLICK_S + period / 2) % period - period / 2) for b in beats)


# --- tempo and beats --------------------------------------------------------


@pytest.mark.parametrize("bpm", [120, 90])
def test_click_track_tempo_and_beat_grid(bpm):
    result = analyze_audio(make_wav(click_samples(bpm)))
    assert result["bpm"] == pytest.approx(bpm, abs=2)
    assert result["bpm_confidence"] > 0.5
    assert len(result["beats"]) >= int(12 / (60 / bpm)) - 1
    assert max_grid_error(result["beats"], bpm) < 0.050


def test_beat_spacing_matches_tempo():
    result = analyze_audio(make_wav(click_samples(120)))
    times = [b["t"] for b in result["beats"]]
    gaps = np.diff(times)
    assert gaps.mean() == pytest.approx(0.5, abs=0.005)


def test_onsets_land_on_the_clicks():
    result = analyze_audio(make_wav(click_samples(120)))
    assert len(result["onsets"]) == 24  # clicks every 0.5 s from 0.25 s to 11.75 s
    for o in result["onsets"]:
        assert 0.0 < o["strength"] <= 1.0
        assert abs((o["t"] - FIRST_CLICK_S + 0.25) % 0.5 - 0.25) < 0.050


@pytest.mark.parametrize("channels,bits", [(1, 16), (2, 16), (1, 24), (2, 24)])
def test_mono_stereo_16_and_24_bit(channels, bits):
    result = analyze_audio(make_wav(click_samples(120), channels=channels, bits=bits))
    assert result["bpm"] == pytest.approx(120, abs=2)
    assert max_grid_error(result["beats"], 120) < 0.050


@pytest.mark.parametrize("bits,tolerance", [(16, 1e-4), (24, 1e-6)])
def test_decoding_preserves_samples_including_negative_ones(bits, tolerance):
    t = np.arange(SR * 3) / SR
    x = 0.7 * np.sin(2 * np.pi * 220 * t)
    mono, rate = music_analysis._decode_wav(make_wav(x, bits=bits))
    assert rate == SR
    assert np.abs(mono - x).max() < tolerance
    assert mono.min() < -0.69
    # Stereo is downmixed by averaging the channels (make_wav puts x and x/2 there).
    stereo, _ = music_analysis._decode_wav(make_wav(x, bits=bits, channels=2))
    assert np.abs(stereo - 0.75 * x).max() < tolerance


def test_other_sample_rate():
    result = analyze_audio(make_wav(click_samples(120, sr=22050), sr=22050))
    assert result["sample_rate"] == 22050
    assert result["bpm"] == pytest.approx(120, abs=2)


def test_noisy_click_track_keeps_its_tempo_but_less_certain():
    clean = analyze_audio(make_wav(click_samples(120)))
    noisy = analyze_audio(make_wav(click_samples(120, noise=0.05)))
    assert noisy["bpm"] == pytest.approx(120, abs=2)
    assert noisy["bpm_confidence"] < clean["bpm_confidence"]


def test_pure_noise_reports_low_confidence_not_a_confident_tempo():
    noise = np.random.default_rng(1).normal(0.0, 0.2, SR * 12)
    result = analyze_audio(make_wav(noise))
    assert result["bpm_confidence"] < 0.3


def test_bpm_range_setting_is_honoured():
    wav = make_wav(click_samples(120))
    assert analyze_audio(wav)["bpm"] == pytest.approx(120, abs=2)
    slow = analyze_audio(wav, settings={"min_bpm": 60, "max_bpm": 100})
    assert slow["bpm"] == pytest.approx(60, abs=2)
    assert 60 <= slow["bpm"] <= 100
    assert slow["settings_used"]["max_bpm"] == 100.0


def test_bpm_range_bounds_the_result_at_the_top_too():
    # 140 BPM clicks, but only 60-100 allowed: the answer must come from that window.
    slow = analyze_audio(make_wav(click_samples(140)), settings={"min_bpm": 60, "max_bpm": 100})
    assert 60 <= slow["bpm"] <= 100
    fast = analyze_audio(make_wav(click_samples(90)), settings={"min_bpm": 120, "max_bpm": 200})
    assert 120 <= fast["bpm"] <= 200


def test_default_settings_are_reported():
    used = analyze_audio(make_wav(click_samples(120)))["settings_used"]
    assert used == {"min_bpm": 60.0, "max_bpm": 200.0, "beats_per_bar": 4}


def test_a_held_tone_has_no_tempo():
    t = np.arange(SR * 4) / SR
    result = analyze_audio(make_wav(0.5 * np.sin(2 * np.pi * 440 * t)))
    assert result["bpm"] is None
    assert result["bpm_confidence"] == 0.0
    assert result["beats"] == []
    assert result["bars"]["times"] == []


# --- bars -------------------------------------------------------------------


def accented_click_samples(bpm=120, per_bar=4, seconds=20.0):
    """Clicks where the first of every bar is 3x louder."""
    x = click_samples(bpm, seconds=seconds)
    period = 60.0 / bpm
    burst_len = int(0.02 * SR)
    k = 0
    t = FIRST_CLICK_S
    while t < seconds - 0.03:
        if k % per_bar == 0:
            i = round(t * SR)
            x[i : i + burst_len] *= 3.0
        k += 1
        t += period
    return np.clip(x, -1.0, 1.0)


def test_accented_downbeat_is_found_with_confidence():
    result = analyze_audio(make_wav(accented_click_samples()))
    bars = result["bars"]
    assert bars["beats_per_bar"] == 4
    assert bars["confidence"] > 0.3
    # Bar starts are every 4th beat, 2.0 s apart, starting on the first click.
    assert np.diff(bars["times"]).mean() == pytest.approx(2.0, abs=0.01)
    assert abs(bars["times"][0] - FIRST_CLICK_S) < 0.050


def test_bars_follow_beats_per_bar_setting():
    result = analyze_audio(make_wav(click_samples(120)), settings={"beats_per_bar": 3})
    assert result["bars"]["beats_per_bar"] == 3
    assert np.diff(result["bars"]["times"]).mean() == pytest.approx(1.5, abs=0.01)


def test_flat_accents_give_low_bar_confidence():
    result = analyze_audio(make_wav(click_samples(120)))
    assert result["bars"]["confidence"] < 0.3


# --- determinism and serialisation ------------------------------------------


def test_same_input_twice_gives_identical_result():
    wav = make_wav(click_samples(90, noise=0.02))
    assert analyze_audio(wav) == analyze_audio(wav)
    assert json.dumps(analyze_audio(wav)) == json.dumps(analyze_audio(wav))


def test_result_is_json_round_trippable():
    result = analyze_audio(make_wav(click_samples(120)))
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    assert result["analyzer_version"] == ANALYZER_VERSION
    assert result["duration_s"] == pytest.approx(12.0, abs=0.001)
    assert result["sample_rate"] == SR


def test_times_are_rounded_to_milliseconds():
    result = analyze_audio(make_wav(click_samples(90)))
    times = [b["t"] for b in result["beats"]] + [o["t"] for o in result["onsets"]]
    times += result["bars"]["times"]
    assert times
    for t in times:
        assert t == round(t, 3)


# --- untrusted input --------------------------------------------------------


def test_silence_is_rejected():
    with pytest.raises(MusicAnalysisError) as exc:
        analyze_audio(make_wav(np.zeros(SR * 5)))
    assert exc.value.code == "silent"


def test_near_silence_is_rejected():
    hiss = np.random.default_rng(3).normal(0.0, 1e-5, SR * 5)
    with pytest.raises(MusicAnalysisError) as exc:
        analyze_audio(make_wav(hiss))
    assert exc.value.code == "silent"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"RIFF",
        b"not audio at all, just text pretending to be a .wav file",
        b"RIFF\x00\x00\x00\x00WAVX" + b"\x00" * 64,
        b"RIFX\x00\x00\x00\x00WAVE" + b"\x00" * 64,
        b"ID3\x04\x00\x00" + b"\x00" * 200,  # an MP3 tag, not a WAV
    ],
)
def test_bad_header_is_rejected(data):
    with pytest.raises(MusicAnalysisError) as exc:
        analyze_audio(data)
    assert exc.value.code == "not_wav"


def test_oversized_file_is_rejected(monkeypatch):
    wav = make_wav(click_samples(120))
    monkeypatch.setattr(music_analysis, "MAX_BYTES", len(wav) - 1)
    with pytest.raises(MusicAnalysisError) as exc:
        analyze_audio(wav)
    assert exc.value.code == "too_large"
    monkeypatch.setattr(music_analysis, "MAX_BYTES", len(wav))
    assert analyze_audio(wav)["bpm"] == pytest.approx(120, abs=2)


def test_the_default_size_cap_is_set():
    assert 0 < music_analysis.MAX_BYTES <= 128 * 1024 * 1024
    assert 0 < music_analysis.MAX_DURATION_S <= 3600


def test_overlong_audio_is_rejected_before_decoding():
    # 11 minutes of 8 kHz mono: well under the byte cap, over the duration cap.
    seconds = int(music_analysis.MAX_DURATION_S) + 60
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(b"\x01\x00" * 8000 * seconds)
    with pytest.raises(MusicAnalysisError) as exc:
        analyze_audio(buf.getvalue())
    assert exc.value.code == "too_long"


def test_too_short_audio_is_rejected():
    with pytest.raises(MusicAnalysisError) as exc:
        analyze_audio(make_wav(click_samples(120, seconds=1.0)))
    assert exc.value.code == "too_short"


def test_empty_audio_is_rejected():
    with pytest.raises(MusicAnalysisError) as exc:
        analyze_audio(make_wav(np.zeros(0)))
    assert exc.value.code == "corrupt"


def test_truncated_audio_data_is_rejected():
    wav = make_wav(click_samples(120))
    with pytest.raises(MusicAnalysisError) as exc:
        analyze_audio(wav[: len(wav) // 2])
    assert exc.value.code == "corrupt"


def test_unsupported_formats_are_rejected():
    def raw(bits, channels, sr=SR):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(bits // 8)
            wf.setframerate(sr)
            wf.writeframes(b"\x10" * (bits // 8) * channels * sr * 3)
        return buf.getvalue()

    for data in (raw(8, 1), raw(32, 1), raw(16, 6), raw(16, 1, sr=1000)):
        with pytest.raises(MusicAnalysisError) as exc:
            analyze_audio(data)
        assert exc.value.code == "unsupported"


@pytest.mark.parametrize(
    "settings",
    [
        {"min_bpm": 120, "max_bpm": 100},
        {"min_bpm": 10},
        {"max_bpm": 1000},
        {"min_bpm": 100, "max_bpm": 105},
        {"beats_per_bar": 1},
        {"beats_per_bar": 4.5},
        {"min_bpm": "fast"},
        {"min_bpm": float("nan")},
        {"colour": "red"},
    ],
)
def test_bad_settings_are_rejected(settings):
    with pytest.raises(MusicAnalysisError) as exc:
        analyze_audio(make_wav(click_samples(120)), settings=settings)
    assert exc.value.code == "bad_settings"


def test_extension_is_irrelevant_only_the_header_counts():
    # bytes of a valid WAV are accepted whatever it was called; there is no filename input.
    assert analyze_audio(bytearray(make_wav(click_samples(120))))["bpm"] == pytest.approx(120, abs=2)


# --- snap_to_grid -----------------------------------------------------------


def test_snap_to_nearest_mark():
    marks = [0.0, 0.5, 1.0, 1.5]
    assert snap_to_grid(0.6, marks) == 0.5
    assert snap_to_grid(0.8, marks) == 1.0
    assert snap_to_grid(-3.0, marks) == 0.0
    assert snap_to_grid(9.0, marks) == 1.5


def test_snap_tie_goes_to_earlier_mark():
    assert snap_to_grid(0.75, [0.5, 1.0]) == 0.5


def test_snap_respects_max_shift():
    marks = [0.0, 1.0]
    assert snap_to_grid(0.1, marks, max_shift_s=0.2) == 0.0
    assert snap_to_grid(0.3, marks, max_shift_s=0.2) == 0.3  # too far: unchanged
    assert snap_to_grid(0.2, marks, max_shift_s=0.2) == 0.0  # exactly at the limit snaps


def test_snap_with_no_marks_returns_t():
    assert snap_to_grid(1.234, []) == 1.234


def test_snap_accepts_unsorted_marks_and_does_not_mutate_them():
    marks = [1.5, 0.0, 1.0, 0.5]
    assert snap_to_grid(0.9, marks) == 1.0
    assert marks == [1.5, 0.0, 1.0, 0.5]


def test_snap_rejects_bad_arguments():
    with pytest.raises(ValueError):
        snap_to_grid(float("nan"), [0.0])
    with pytest.raises(ValueError):
        snap_to_grid(0.0, [0.0], max_shift_s=-1.0)


def test_snap_to_analysed_beats():
    result = analyze_audio(make_wav(click_samples(120)))
    beats = [b["t"] for b in result["beats"]]
    snapped = snap_to_grid(3.07, beats)
    assert abs(snapped - 3.25) < 0.05 and snapped in beats
