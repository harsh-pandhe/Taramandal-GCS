import { describe, it, expect } from 'vitest';
import {
  makeLedKeyframe, evaluateKeyframe, ledColorAt, hexToRgbw, serializeTrack, PATTERN_VALUES,
} from './ledPatterns.js';

// Colours come back as THREE.Color (0..1 floats); compare in 0..255 ints so
// these read the same way as the Python tests they mirror.
const rgb255 = (c) => [c.r, c.g, c.b].map((v) => Math.round(v * 255));

describe('hexToRgbw', () => {
  it.each([
    ['#fff', [255, 255, 255, 0]],
    ['#0080ff', [0, 128, 255, 0]],
    ['#0080ff40', [0, 128, 255, 64]],
  ])('parses %s', (hex, expected) => {
    const { r, g, b, w } = hexToRgbw(hex);
    expect([r, g, b, w]).toEqual(expected);
  });

  it('returns black for malformed input rather than NaN', () => {
    expect(hexToRgbw('#zzz')).toEqual({ r: 0, g: 0, b: 0, w: 0 });
    expect(hexToRgbw('')).toEqual({ r: 0, g: 0, b: 0, w: 0 });
  });
});

describe('patterns match led_engine.py', () => {
  it('SOLID is constant', () => {
    const kf = makeLedKeyframe({ pattern: 'SOLID', color: '#123456', duration: 10 });
    for (const t of [0, 1.7, 9.9]) expect(rgb255(evaluateKeyframe(kf, t))).toEqual([18, 52, 86]);
  });

  it('COLOR_CYCLE hits its stops and loops', () => {
    const kf = makeLedKeyframe({
      pattern: 'COLOR_CYCLE', duration: 4, palette: ['#ff0000', '#00ff00'],
    });
    expect(rgb255(evaluateKeyframe(kf, 0))).toEqual([255, 0, 0]);
    expect(rgb255(evaluateKeyframe(kf, 2))).toEqual([0, 255, 0]);
    // Wraps back toward the first stop instead of dead-ending.
    expect(rgb255(evaluateKeyframe(kf, 3.99))[0]).toBeGreaterThan(240);
  });

  it.each([2, 4, 8])('STROBE at %iHz holds its duty cycle', (freq) => {
    const kf = makeLedKeyframe({
      pattern: 'STROBE', duration: 10, color: '#ffffff', frequency_hz: freq, duty_cycle: 0.25,
    });
    let lit = 0;
    const N = 10000;
    for (let i = 0; i < N; i += 1) if (evaluateKeyframe(kf, i / 1000 + 1e-6).r > 0) lit += 1;
    expect(lit / N).toBeCloseTo(0.25, 1);
  });

  it('STROBE phase is relative to the keyframe start', () => {
    const kf = makeLedKeyframe({
      pattern: 'STROBE', start_time: 5, duration: 5, color: '#ffffff', frequency_hz: 4,
    });
    expect(evaluateKeyframe(kf, 5).r).toBe(1);
  });

  it('SPARKLE lights roughly the requested density and is stable', () => {
    const kf = makeLedKeyframe({
      pattern: 'SPARKLE', duration: 10, color: '#ffffff', density: 0.3,
    });
    const lit = [];
    for (let i = 0; i < 500; i += 1) lit.push(evaluateKeyframe(kf, 1, i, 500).r > 0);
    expect(lit.filter(Boolean).length / 500).toBeCloseTo(0.3, 1);
    // Same inputs must give the same answer, or the preview would shimmer
    // independently of the show.
    for (let i = 0; i < 500; i += 1) {
      expect(evaluateKeyframe(kf, 1, i, 500).r > 0).toBe(lit[i]);
    }
  });

  it('WAVE_PROPAGATION peaks at the crest and troughs half a wavelength away', () => {
    const kf = makeLedKeyframe({
      pattern: 'WAVE_PROPAGATION', duration: 10, color: '#00ff00',
      axis: 'z', wave_length: 10, wave_speed: 5,
    });
    const at = (z) => evaluateKeyframe(kf, 0, 0, 4, { x: 0, y: 0, z }).g;
    expect(at(0)).toBeGreaterThan(0.9);
    expect(at(5)).toBeLessThan(0.05);
  });

  it('WAVE_PROPAGATION travels over time', () => {
    const kf = makeLedKeyframe({
      pattern: 'WAVE_PROPAGATION', duration: 10, color: '#00ff00',
      axis: 'z', wave_length: 10, wave_speed: 5,
    });
    const pos = { x: 0, y: 0, z: 5 };
    expect(evaluateKeyframe(kf, 0, 0, 4, pos).g).toBeLessThan(0.05);
    expect(evaluateKeyframe(kf, 1, 0, 4, pos).g).toBeGreaterThan(0.9);
  });

  it('every declared pattern evaluates to a colour', () => {
    for (const pattern of PATTERN_VALUES) {
      const c = evaluateKeyframe(makeLedKeyframe({ pattern, duration: 5 }), 1, 0, 4, { x: 0, y: 0, z: 0 });
      expect(Number.isFinite(c.r) && Number.isFinite(c.g) && Number.isFinite(c.b)).toBe(true);
    }
  });
});

describe('ledColorAt track composition', () => {
  it('is dark outside every keyframe', () => {
    const track = [makeLedKeyframe({ pattern: 'SOLID', start_time: 2, duration: 2, color: '#ffffff' })];
    expect(rgb255(ledColorAt(track, 0))).toEqual([0, 0, 0]);
    expect(rgb255(ledColorAt(track, 5))).toEqual([0, 0, 0]);
    expect(rgb255(ledColorAt(track, 3))).toEqual([255, 255, 255]);
  });

  it('treats a keyframe window as half-open', () => {
    const track = [
      makeLedKeyframe({ pattern: 'SOLID', start_time: 0, duration: 2, color: '#ff0000' }),
      makeLedKeyframe({ pattern: 'SOLID', start_time: 2, duration: 2, color: '#00ff00' }),
    ];
    expect(rgb255(ledColorAt(track, 1.99))).toEqual([255, 0, 0]);
    expect(rgb255(ledColorAt(track, 2))).toEqual([0, 255, 0]);
  });

  it('lets a later keyframe win on overlap', () => {
    const track = [
      makeLedKeyframe({ pattern: 'SOLID', start_time: 0, duration: 10, color: '#ff0000' }),
      makeLedKeyframe({ pattern: 'SOLID', start_time: 1, duration: 1, color: '#0000ff' }),
    ];
    expect(rgb255(ledColorAt(track, 1.5))).toEqual([0, 0, 255]);
    expect(rgb255(ledColorAt(track, 5))).toEqual([255, 0, 0]);
  });

  it('handles an empty or missing track', () => {
    expect(rgb255(ledColorAt([], 1))).toEqual([0, 0, 0]);
    expect(rgb255(ledColorAt(null, 1))).toEqual([0, 0, 0]);
  });
});

describe('serializeTrack', () => {
  it('drops the UI-only id so the payload matches LedKeyframe', () => {
    const out = serializeTrack([makeLedKeyframe({ pattern: 'SOLID' })]);
    expect(out[0]).not.toHaveProperty('id');
    expect(out[0].pattern).toBe('SOLID');
  });
});
