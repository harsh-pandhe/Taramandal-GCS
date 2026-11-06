import { describe, it, expect } from 'vitest';
import { buildAutoLightTrack } from './autoLight.js';
import { ledColorAt } from './ledPatterns.js';

// Eleven drones in a row from x = -10 to +10, standing still the whole show.
const fleet = Array.from({ length: 11 }, (_, i) => ({
  id: i, waypoints: Array.from({ length: 241 }, (_, k) => ({ time: k / 10, x: -10 + 2 * i, y: 0, z: -20 })),
}));
const show = { drones: fleet };
const segments = [
  { kind: 'takeoff', label: 'Takeoff', start: 0, end: 5 },
  { kind: 'morph', label: 'Climb-out', start: 5, end: 10 },
  { kind: 'hold', label: 'A', start: 10, end: 20 },
  { kind: 'morph', label: 'Morph → B', start: 20, end: 24 },
  { kind: 'hold', label: 'B', start: 24, end: 24.1 },
  { kind: 'land', label: 'Landing', start: 24.1, end: 24.1 },
];
const on = (c) => c.r + c.g + c.b > 0.02;

describe('auto light show', () => {
  it('lights only the holds: a sweep then a running colour, nothing for the flying legs', () => {
    const track = buildAutoLightTrack(show, segments);
    expect(track.map((k) => k.pattern)).toEqual(['SWEEP_ON', 'COLOR_CHASE']);   // the 0.1 s hold is skipped
    expect(track[0].start_time).toBe(10);
    expect(track[1].start_time + track[1].duration).toBeCloseTo(20, 5);
    for (const t of [0, 4, 7, 9.9, 20.5, 23]) expect(on(ledColorAt(track, t, 5, 11, { x: 0 }))).toBe(false);
  });

  it('sweeps left to right: the left drone lights first, the right drone last', () => {
    const [sweep] = buildAutoLightTrack(show, segments, { direction: 'ltr', sweepSeconds: 2.5 });
    expect(sweep.reverse).toBe(false);
    expect(sweep.axis_origin).toBe(-10);
    expect(on(ledColorAt([sweep], 10.01, 0, 11, { x: -10 }))).toBe(true);
    expect(on(ledColorAt([sweep], 10.01, 10, 11, { x: 10 }))).toBe(false);
    expect(on(ledColorAt([sweep], 12.6, 10, 11, { x: 10 }))).toBe(true);
  });

  it('sweeps right to left on request, and alternates scene by scene', () => {
    const [rtl] = buildAutoLightTrack(show, segments, { direction: 'rtl' });
    expect(rtl.reverse).toBe(true);
    expect(rtl.axis_origin).toBe(10);
    expect(on(ledColorAt([rtl], 10.01, 10, 11, { x: 10 }))).toBe(true);
    expect(on(ledColorAt([rtl], 10.01, 0, 11, { x: -10 }))).toBe(false);
    const two = [
      { kind: 'hold', label: 'A', start: 10, end: 20 }, { kind: 'morph', label: 'm', start: 20, end: 22 },
      { kind: 'hold', label: 'B', start: 22, end: 32 },
    ];
    const alt = buildAutoLightTrack(show, two, { direction: 'alternate' }).filter((k) => k.pattern === 'SWEEP_ON');
    expect(alt.map((k) => k.reverse)).toEqual([false, true]);
  });

  it('every drone blinks as the front reaches it: dark, lit, dark, lit, then steady', () => {
    const [sweep] = buildAutoLightTrack(show, segments, { blinkCount: 3, blinkHz: 4 });
    const at = (age) => on(ledColorAt([sweep], 10 + age, 0, 11, { x: -10 }));   // the first drone, reached at once
    expect([at(0.0), at(0.15), at(0.26), at(0.4), at(0.51), at(0.65), at(0.9), at(2)]).toEqual([true, false, true, false, true, false, true, true]);
  });

  it('returns nothing for an empty show or no timeline', () => {
    expect(buildAutoLightTrack(null, segments)).toEqual([]);
    expect(buildAutoLightTrack(show, [])).toEqual([]);
  });
});
