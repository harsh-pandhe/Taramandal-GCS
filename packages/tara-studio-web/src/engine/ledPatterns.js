// LED keyframe patterns — the browser-side twin of tara_studio/led_engine.py.
//
// The Python module is the source of truth for what gets *exported*; this one
// exists so the viewport can preview a track at 60fps without a round trip.
// The two must agree, so the maths here mirrors led_engine.py exactly —
// including SPARKLE's hashed (not random) selection, which is what lets a
// preview and a compiled .tara show the same drones lit at the same instant.

import * as THREE from 'three';

export const PATTERNS = [
  { value: 'SOLID', label: 'Solid' },
  { value: 'COLOR_CYCLE', label: 'Colour Cycle' },
  { value: 'STROBE', label: 'Strobe' },
  { value: 'SPARKLE', label: 'Sparkle' },
  { value: 'WAVE_PROPAGATION', label: 'Wave' },
];

export const PATTERN_VALUES = PATTERNS.map((p) => p.value);

/** Defaults for a newly dropped keyframe. Mirrors LedKeyframe's dataclass defaults. */
export const makeLedKeyframe = (patch = {}) => ({
  id: `led-${Math.random().toString(36).slice(2, 10)}`,
  pattern: 'SOLID',
  start_time: 0,
  duration: 4,
  color: '#38bdf8',
  palette: ['#ff0055', '#38bdf8', '#22c55e'],
  frequency_hz: 4,
  duty_cycle: 0.5,
  density: 0.15,
  axis: 'z',
  wave_speed: 4,
  wave_length: 12,
  ...patch,
});

const clamp01 = (v) => Math.max(0, Math.min(1, v));

/** #RGB / #RRGGBB / #RRGGBBWW -> {r,g,b,w} in 0..255. */
export const hexToRgbw = (value) => {
  let s = String(value || '').replace('#', '');
  if (s.length === 3) s = s.split('').map((c) => c + c).join('');
  if (s.length === 6) s += '00';
  if (s.length !== 8 || /[^0-9a-fA-F]/.test(s)) return { r: 0, g: 0, b: 0, w: 0 };
  return {
    r: parseInt(s.slice(0, 2), 16),
    g: parseInt(s.slice(2, 4), 16),
    b: parseInt(s.slice(4, 6), 16),
    w: parseInt(s.slice(6, 8), 16),
  };
};

/**
 * Deterministic 0..1 noise — the JS twin of led_engine._hash01.
 *
 * Not the same hash function as Python's blake2b, so the *set* of sparkling
 * drones differs between preview and export; the density and the
 * frame-to-frame stability match, which is what a designer is judging. Making
 * them bit-identical would mean shipping a blake2b to the browser for a
 * cosmetic effect.
 */
const hash01 = (a, b) => {
  let h = (Math.imul(a, 0x27d4eb2d) ^ Math.imul(b, 0x165667b1)) >>> 0;
  h ^= h >>> 15;
  h = Math.imul(h, 0x2545f491) >>> 0;
  h ^= h >>> 13;
  return (h >>> 0) / 0xffffffff;
};

/**
 * A drone's colour under one keyframe, as a THREE.Color.
 *
 * @param {object} kf        keyframe
 * @param {number} t         absolute show time (s)
 * @param {number} droneIdx
 * @param {number} numDrones
 * @param {object} [pos]     {x,y,z} — required for WAVE_PROPAGATION
 */
export const evaluateKeyframe = (kf, t, droneIdx = 0, numDrones = 1, pos = null) => {
  const local = t - kf.start_time;
  const base = hexToRgbw(kf.color);
  const c = new THREE.Color();

  switch (kf.pattern) {
    case 'COLOR_CYCLE': {
      const stops = (kf.palette && kf.palette.length ? kf.palette : [kf.color]);
      if (stops.length === 1) return c.set(stops[0]);
      const ring = [...stops, stops[0]];
      const span = kf.duration > 0 ? kf.duration : 1;
      const u = ((local / span) % 1 + 1) % 1;
      const p = u * (ring.length - 1);
      const i = Math.floor(p);
      const a = new THREE.Color(ring[i]);
      const b = new THREE.Color(ring[i + 1]);
      return a.lerp(b, p - i);
    }
    case 'STROBE': {
      const freq = Math.max(0.01, kf.frequency_hz);
      const duty = clamp01(kf.duty_cycle);
      const phase = ((local * freq) % 1 + 1) % 1;
      return phase < duty ? c.set(kf.color) : c.setRGB(0, 0, 0);
    }
    case 'SPARKLE': {
      const bucket = Math.floor(local * 12);
      return hash01(droneIdx, bucket) < clamp01(kf.density)
        ? c.set(kf.color) : c.setRGB(0, 0, 0);
    }
    case 'WAVE_PROPAGATION': {
      const coord = pos && kf.axis in pos ? Number(pos[kf.axis]) : droneIdx;
      const wl = kf.wave_length || 1;
      const phase = (coord - kf.wave_speed * local) / wl;
      const amp = 0.5 * (1 + Math.cos(2 * Math.PI * phase));
      // Scale value in HSL, matching led_engine's HSV modulation: the hue the
      // designer picked survives, only brightness travels.
      c.set(kf.color);
      const hsl = {};
      c.getHSL(hsl);
      return c.setHSL(hsl.h, hsl.s, hsl.l * amp);
    }
    case 'SOLID':
    default:
      return c.setRGB(base.r / 255, base.g / 255, base.b / 255);
  }
};

/** Last keyframe covering `t` wins; uncovered time is dark. Mirrors color_at(). */
export const ledColorAt = (track, t, droneIdx = 0, numDrones = 1, pos = null) => {
  let active = null;
  for (const kf of track || []) {
    // Half-open [start, end) so abutting keyframes never both match.
    if (t >= kf.start_time && t < kf.start_time + kf.duration) active = kf;
  }
  if (!active) return new THREE.Color(0, 0, 0);
  return evaluateKeyframe(active, t, droneIdx, numDrones, pos);
};

/** Strips UI-only fields before sending a track to the backend. */
export const serializeTrack = (track) =>
  (track || []).map(({ id: _id, ...rest }) => rest);
