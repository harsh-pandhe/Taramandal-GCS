"""
Taramandal Studio - LED Pattern Engine

Evaluates a drone's RGBW colour at an instant in the show. Patterns are
sampled per (drone, time) rather than baked per keyframe, so the same call
serves the live Studio preview, the binary exporter, and any offline
re-render without the three drifting apart.

Colour space note: everything here works in 0-255 integer RGBW. The web
preview (engine/lighting.js) works in floats and converts at the edge; keeping
the Python side integral means the exporter never re-quantises and the
checksum over a compiled show is stable across runs.
"""

from __future__ import annotations

import colorsys
import hashlib
import math
import struct
from dataclasses import dataclass, field
from typing import Optional, Sequence

from tara_core import interpolate_waypoint

# Pattern identifiers. These strings are the wire format - they appear in
# project JSON and in the .tara header's pattern table, so renaming one is a
# breaking change to saved shows.
SOLID = "SOLID"
COLOR_CYCLE = "COLOR_CYCLE"
STROBE = "STROBE"
SPARKLE = "SPARKLE"
WAVE_PROPAGATION = "WAVE_PROPAGATION"

PATTERNS = (SOLID, COLOR_CYCLE, STROBE, SPARKLE, WAVE_PROPAGATION)


def hex_to_rgbw(value: str) -> tuple[int, int, int, int]:
    """Parses #RGB, #RRGGBB or #RRGGBBWW into an (r,g,b,w) tuple.

    The white channel defaults to 0 rather than being derived from the RGB
    minimum: these are additive RGBW fixtures, so auto-filling W would make
    every colour brighter than the designer picked.
    """
    s = value.lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) == 6:
        s += "00"
    if len(s) != 8:
        raise ValueError(f"bad colour {value!r}: expected #RGB, #RRGGBB or #RRGGBBWW")
    try:
        n = int(s, 16)
    except ValueError as exc:
        raise ValueError(f"bad colour {value!r}: not hexadecimal") from exc
    return ((n >> 24) & 255, (n >> 16) & 255, (n >> 8) & 255, n & 255)


def _clamp8(v: float) -> int:
    """Rounds to the nearest integer and clamps into a uint8."""
    return max(0, min(255, int(round(v))))


def _lerp(a: float, b: float, u: float) -> float:
    return a + (b - a) * u


def _hash01(*parts: int) -> float:
    """Deterministic 0..1 noise from integer inputs.

    SPARKLE must look random but replay identically every time a show is
    compiled - a designer previewing a sparkle and the exporter baking it have
    to agree, and a re-export must produce a byte-identical file. A hash of the
    inputs gives that; random.random() would not.
    """
    h = hashlib.blake2b(struct.pack(f"<{len(parts)}i", *parts), digest_size=4)
    return int.from_bytes(h.digest(), "little") / 0xFFFFFFFF


@dataclass
class LedKeyframe:
    """One lighting instruction covering [start_time, start_time + duration).

    Attributes:
        pattern: one of PATTERNS.
        start_time: show-relative seconds.
        duration: seconds; the keyframe is active for this long.
        color: base colour, "#RRGGBB" or "#RRGGBBWW". Used by SOLID/STROBE/
            SPARKLE, and as the fallback for an empty COLOR_CYCLE palette.
        palette: COLOR_CYCLE's colour ramp; interpolated in RGB, wrapping back
            to palette[0] so a cycle loops seamlessly.
        frequency_hz: STROBE flash rate (2-8 Hz per spec).
        duty_cycle: STROBE on-fraction, 0..1.
        density: SPARKLE - fraction of the swarm lit at any instant, 0..1.
        axis: WAVE_PROPAGATION travel axis, "x" | "y" | "z".
        wave_speed: wave travel speed in metres/second.
        wave_length: crest-to-crest distance in metres.
    """

    pattern: str = SOLID
    start_time: float = 0.0
    duration: float = 0.0
    color: str = "#FFFFFF"
    palette: list[str] = field(default_factory=list)
    frequency_hz: float = 4.0
    duty_cycle: float = 0.5
    density: float = 0.15
    axis: str = "z"
    wave_speed: float = 4.0
    wave_length: float = 12.0

    def __post_init__(self) -> None:
        if self.pattern not in PATTERNS:
            raise ValueError(f"unknown pattern {self.pattern!r}; expected one of {list(PATTERNS)}")
        if self.axis not in ("x", "y", "z"):
            raise ValueError(f"bad axis {self.axis!r}: expected 'x', 'y' or 'z'")
        if self.duration < 0:
            raise ValueError("duration must be >= 0")

    @property
    def end_time(self) -> float:
        return self.start_time + self.duration

    def contains(self, t: float) -> bool:
        """Half-open [start, end) so abutting keyframes never both match."""
        return self.start_time <= t < self.end_time

    @classmethod
    def from_dict(cls, d: dict) -> "LedKeyframe":
        """Builds from project JSON, ignoring unknown keys."""
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})


def evaluate(
    kf: LedKeyframe,
    t: float,
    drone_index: int = 0,
    num_drones: int = 1,
    position: Optional[dict] = None,
) -> tuple[int, int, int, int]:
    """A single drone's (r,g,b,w) under one keyframe at absolute show time `t`.

    `t` is absolute; the local phase is taken as t - kf.start_time so a pattern
    always begins at its own start rather than at show zero.
    """
    local = t - kf.start_time
    base = hex_to_rgbw(kf.color)

    if kf.pattern == SOLID:
        return base

    if kf.pattern == COLOR_CYCLE:
        stops = kf.palette or [kf.color]
        if len(stops) == 1:
            return hex_to_rgbw(stops[0])
        # Wrap back to the first stop so the ramp is a loop, not a dead end.
        ring = [hex_to_rgbw(c) for c in stops] + [hex_to_rgbw(stops[0])]
        span = kf.duration if kf.duration > 0 else 1.0
        u = (local / span) % 1.0
        pos = u * (len(ring) - 1)
        i = int(pos)
        f = pos - i
        a, b = ring[i], ring[i + 1]
        return tuple(_clamp8(_lerp(a[k], b[k], f)) for k in range(4))

    if kf.pattern == STROBE:
        freq = max(0.01, kf.frequency_hz)
        duty = max(0.0, min(1.0, kf.duty_cycle))
        phase = (local * freq) % 1.0
        return base if phase < duty else (0, 0, 0, 0)

    if kf.pattern == SPARKLE:
        density = max(0.0, min(1.0, kf.density))
        # Re-roll which drones are lit at a fixed rate rather than per frame,
        # so a sparkle reads as discrete twinkles instead of white noise that
        # changes with the export sample rate.
        bucket = int(local * 12.0)
        return base if _hash01(drone_index, bucket) < density else (0, 0, 0, 0)

    if kf.pattern == WAVE_PROPAGATION:
        # Colour wave travelling along an axis. Drones without a position fall
        # back to their index so the effect still reads on an unplaced fleet.
        if position is not None and kf.axis in position:
            coord = float(position[kf.axis])
        else:
            coord = float(drone_index)
        wavelength = kf.wave_length if kf.wave_length else 1.0
        phase = (coord - kf.wave_speed * local) / wavelength
        # Cosine raised into 0..1: a smooth crest rather than a hard edge.
        amp = 0.5 * (1.0 + math.cos(2.0 * math.pi * phase))
        r, g, b, w = base
        # Modulate value in HSV so the hue the designer chose is preserved and
        # only brightness travels - scaling RGB directly desaturates the crest.
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        rr, gg, bb = colorsys.hsv_to_rgb(h, s, v * amp)
        return (_clamp8(rr * 255), _clamp8(gg * 255), _clamp8(bb * 255), _clamp8(w * amp))

    raise ValueError(f"unhandled pattern {kf.pattern!r}")


def color_at(
    keyframes: Sequence[LedKeyframe],
    t: float,
    drone_index: int = 0,
    num_drones: int = 1,
    position: Optional[dict] = None,
) -> tuple[int, int, int, int]:
    """Colour from the last keyframe covering `t`.

    Later keyframes win on overlap, which makes a track behave the way a
    designer expects when they drop a strobe on top of an existing solid. Time
    outside every keyframe is dark.
    """
    active = None
    for kf in keyframes:
        if kf.contains(t):
            active = kf
    if active is None:
        return (0, 0, 0, 0)
    return evaluate(active, t, drone_index, num_drones, position)


def build_track(keyframes: Sequence[dict] | Sequence[LedKeyframe]) -> list[LedKeyframe]:
    """Normalises a mixed list of dicts/LedKeyframes into LedKeyframes."""
    return [kf if isinstance(kf, LedKeyframe) else LedKeyframe.from_dict(kf) for kf in keyframes]


#: Ceiling on the rate a strobe asks for. The show is then resampled to the
#: next whole multiple of its own rate (52 Hz from a 4 Hz show). At 50 Hz a
#: 100-drone, 120 s show is ~9.6 MB of .tara records; anything finer would be
#: paying for flashes shorter than the LEDs can meaningfully render.
MAX_LED_SAMPLE_RATE_HZ = 50.0

#: How many samples each strobe on-phase and off-phase should get. One is the
#: bare minimum to see the flash at all, but a sample landing exactly on a
#: phase edge can fall either side under float rounding; two leaves slack.
STROBE_SAMPLES_PER_PHASE = 2

#: The least slack accepted once the rate has hit MAX_LED_SAMPLE_RATE_HZ: the
#: shortest phase must still span 1.25 sample periods, so it always contains
#: one sample with room to spare rather than exactly on an edge.
_MIN_PHASE_SAMPLES = 1.25


def led_sample_rate_hz(keyframes: Sequence[dict] | Sequence[LedKeyframe]) -> float:
    """Lowest uniform sample rate at which every strobe in `keyframes` survives.

    Colours are baked onto waypoints, so they are only ever seen at waypoint
    times. A strobe sampled at or below its own flash rate aliases: a 4 Hz
    strobe on a 4 Hz show lands every sample at the same phase and exports as
    solid light. Returns 0.0 when nothing in the track needs a particular rate.

    Raises:
        ValueError: a strobe's on- or off-phase is too short to be sampled even
            at MAX_LED_SAMPLE_RATE_HZ, so it could not survive export.
    """
    rate = 0.0
    for kf in build_track(keyframes):
        if kf.pattern != STROBE or kf.duration <= 0:
            continue
        freq = max(0.01, kf.frequency_hz)
        duty = max(0.0, min(1.0, kf.duty_cycle))
        # Duty 0 or 1 is constant light, not a strobe.
        shortest = min(duty, 1.0 - duty) / freq
        if shortest <= 0:
            continue
        needed = STROBE_SAMPLES_PER_PHASE / shortest
        if needed > MAX_LED_SAMPLE_RATE_HZ:
            # The epsilon keeps 1 - 0.8 = 0.19999... from refusing a legal 25 ms phase.
            if shortest * MAX_LED_SAMPLE_RATE_HZ < _MIN_PHASE_SAMPLES - 1e-9:
                raise ValueError(
                    f"strobe at {kf.start_time:g}s ({freq:g} Hz, duty {duty:g}) has a "
                    f"{shortest * 1000:.0f} ms phase, too short to export at "
                    f"{MAX_LED_SAMPLE_RATE_HZ:g} Hz; lower the rate or move the duty "
                    f"towards 0.5"
                )
            needed = MAX_LED_SAMPLE_RATE_HZ
        rate = max(rate, needed)
    return rate


def upsample_factor(choreo: dict, rate_hz: float) -> int:
    """How many pieces each waypoint interval must be split into for `rate_hz`.

    One factor for the whole show, sized by its longest interval, so a show
    sampled at a fixed rate stays at a fixed rate (just a multiple of it) -
    the .tara player seeks by multiplying, and needs uniform frames.
    """
    if rate_hz <= 0:
        return 1
    longest = 0.0
    for drone in choreo.get("drones", []):
        times = sorted(wp["time"] for wp in drone.get("waypoints", []))
        for a, b in zip(times, times[1:]):
            longest = max(longest, b - a)
    # The epsilon stops 0.25 s * 16 Hz = 4.000000001 from rounding up to 5.
    return max(1, math.ceil(longest * rate_hz - 1e-9))


def upsample_choreo(choreo: dict, factor: int) -> dict:
    """Returns a copy of `choreo` with every waypoint interval split `factor` ways.

    New waypoints come from tara_core.interpolate_waypoint - the same linear
    position / shorter-arc yaw interpolation the GCS flies between waypoints -
    so they sit exactly on the path the drone was already going to take. The
    original waypoints are kept unchanged; only extra frames are added.
    """
    if factor <= 1:
        return choreo

    out = []
    for drone in choreo.get("drones", []):
        wps = sorted(drone.get("waypoints", []), key=lambda w: w["time"])
        dense = []
        for w1, w2 in zip(wps, wps[1:]):
            dense.append(w1)
            for k in range(1, factor):
                t = w1["time"] + (w2["time"] - w1["time"]) * k / factor
                mid = interpolate_waypoint([w1, w2], t)
                dense.append({
                    **w1,
                    "time": t,
                    "x": mid["x"],
                    "y": mid["y"],
                    "z": mid["z"],
                    "yaw": mid.get("yaw", 0.0),
                })
        if wps:
            dense.append(wps[-1])
        out.append({**drone, "waypoints": dense})

    return {**choreo, "drones": out}


def apply_track_to_choreo(
    choreo: dict,
    keyframes: Sequence[dict] | Sequence[LedKeyframe],
) -> dict:
    """Returns a copy of `choreo` with r/g/b/w written onto every waypoint.

    Position-aware patterns (WAVE_PROPAGATION) read each waypoint's own x/y/z,
    so a wave follows the drones as the formation moves rather than being
    fixed to where they started.
    """
    track = build_track(keyframes)
    drones = choreo.get("drones", [])
    n = len(drones)

    out = []
    for idx, drone in enumerate(drones):
        wps = []
        for wp in drone.get("waypoints", []):
            r, g, b, w = color_at(track, wp["time"], idx, n, wp)
            wps.append({**wp, "r": r, "g": g, "b": b, "w": w})
        out.append({**drone, "waypoints": wps})

    return {**choreo, "drones": out}
