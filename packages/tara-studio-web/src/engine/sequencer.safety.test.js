// PR #141 review follow-up: the 500-drone prediction crash and the guarantee
// that a missing assignment can never switch the safety checks off.
import { describe, it, expect } from 'vitest';
import { SHAPE_LIBRARY } from './formations.js';
import {
  makeScene, computeShowDuration, buildTimelineSegments, buildSequencedShow, predictShow,
  computeAssignmentChainLocal,
  measureExportedGeometry, MorphRefusalError,
} from './sequencer.js';

const one = (entry, over = {}) => makeScene({
  id: `s-${entry.shapePayload}`, name: entry.label || entry.shapePayload,
  shapeType: entry.shapeType, shapePayload: entry.shapePayload, ...over,
});
const heart = one({ shapeType: 'parametric', shapePayload: 'heart' });
const identity = (n) => Array.from({ length: n }, (_, i) => i);
const N500 = 500;

describe('500-drone prediction (PR #141 crash)', () => {
  it('the exact reproduction: strict computeShowDuration refuses with a typed, actionable error', () => {
    let err;
    try { computeShowDuration([heart], N500, 4, null); } catch (e) { err = e; }
    expect(err).toBeInstanceOf(MorphRefusalError);
    expect(err.code).toBe('CEILING');
    expect(err.message).toMatch(/120m ceiling/);
    expect(() => buildTimelineSegments([heart], N500, 4, null)).toThrow(MorphRefusalError);
  });

  it('predictShow (what the render path calls) never throws and says what it is', () => {
    const p = predictShow([heart], N500, 4, null);
    expect(p.basis).toBe('nominal');
    expect(p.refusal).toBeNull();
    expect(p.duration).toBeGreaterThan(0);
    expect(p.segments.length).toBeGreaterThan(0);
  });

  it('every built-in shape (the library has 16, the review cited 15) at 500 drones: predictShow never throws; strict paths throw only typed refusals', () => {
    expect(SHAPE_LIBRARY.length).toBeGreaterThanOrEqual(15);
    let refused = 0;
    for (const entry of SHAPE_LIBRARY) {
      const scenes = [one(entry)];
      expect(() => predictShow(scenes, N500, 4, null), entry.shapePayload).not.toThrow();
      try {
        const d = computeShowDuration(scenes, N500, 4, null);
        expect(Number.isFinite(d) && d > 0, entry.shapePayload).toBe(true);
      } catch (e) {
        expect(e, entry.shapePayload).toBeInstanceOf(MorphRefusalError);
        refused++;
      }
    }
    expect(refused).toBeGreaterThan(0); // the reported crash population really is refused
  }, 120000);

  it('a refused plan for a stale assignment is reported, not hidden and not thrown', () => {
    const p = predictShow([heart], N500, 4, [identity(N500), identity(N500)]);
    expect(p.basis).toBe('nominal');
    expect(p.refusal).toMatch(/cannot be made collision-free/);
  });

  it('a deliberate formation refusal surfaces as a typed refusal', () => {
    const line = one({ shapeType: 'parametric', shapePayload: 'line' });
    let err;
    try { computeShowDuration([line], N500, 4, [identity(N500), identity(N500)]); } catch (e) { err = e; }
    expect(err).toBeInstanceOf(MorphRefusalError);
    expect(err.code).toBe('FORMATION');
  });

  it('the assignment-chain path reports a refused formation as a typed refusal (was a plain Error)', () => {
    const line = one({ shapeType: 'parametric', shapePayload: 'line' });
    let err;
    try { computeAssignmentChainLocal([line], N500); } catch (e) { err = e; }
    expect(err).toBeInstanceOf(MorphRefusalError);
    expect(err.code).toBe('FORMATION');
  });

  it('unexpected failures are not swallowed', () => {
    expect(() => predictShow([null], 5, 4, [identity(5), identity(5)])).toThrow(TypeError);
    expect(() => predictShow([heart], 5, 4, 'nope')).toThrow(TypeError);
    expect(() => computeShowDuration([heart], 5, 4, 'nope')).toThrow(TypeError);
  });
});

describe('assignment handling', () => {
  const circle = one({ shapeType: 'parametric', shapePayload: 'circle' }, { transitionDurationSeconds: 5 });

  it('absent assignment (null and undefined) is handled explicitly as nominal in predictShow', () => {
    for (const t of [null, undefined]) {
      const p = predictShow([circle], 6, 4, t);
      expect(p.basis).toBe('nominal');
      expect(p.refusal).toBeNull();
    }
    expect(predictShow([], 6, 4, null).duration).toBe(0);
    expect(predictShow([circle], null, 4, null).basis).toBe('nominal');
  });

  it('index zero is a valid assignment value, not "missing"', () => {
    const swapped = [1, 0, 2, 3, 4, 5];
    const show = buildSequencedShow([circle], 6, 4, [swapped, identity(6)]);
    expect(show._targetIdx[0]).toEqual(swapped);
    expect(show._targetIdx[1]).toEqual(identity(6));
    const id = buildSequencedShow([circle], 6, 4, [identity(6), identity(6)]);
    expect(id._targetIdx[0][0]).toBe(0);
  });

  it('a corrupt assignment is refused; a merely stale one falls back to identity as documented', () => {
    expect(() => buildSequencedShow([circle], 6, 4, [[0, 0, 2, 3, 4, 5], identity(6)])).toThrow(TypeError);
    expect(() => buildSequencedShow([circle], 6, 4, [[0, 1, 2, 3, 4, 9], identity(6)])).toThrow(TypeError);
    const stale = buildSequencedShow([circle], 6, 4, [identity(3)]); // wrong fleet size
    expect(stale._targetIdx[0]).toEqual(identity(6));
  });

  it('prediction equals the build for the same inputs at normal scale', () => {
    const a = [identity(6), identity(6)];
    const show = buildSequencedShow([circle], 6, 4, a);
    const p = predictShow([circle], 6, 4, a);
    expect(p.basis).toBe('planned');
    expect(p.duration).toBeCloseTo(show._duration, 2);
    expect(computeShowDuration([circle], 6, 4, a)).toBeCloseTo(show._duration, 2);
    expect(p.segments).toEqual(buildTimelineSegments([circle], 6, 4, a));
    expect(p.segments.at(-1).end).toBeCloseTo(show._duration, 1);
  });
});

describe('a missing assignment cannot bypass the safety checks', () => {
  it('buildSequencedShow with no assignment still refuses what the checks refuse (500 drones)', () => {
    expect(() => buildSequencedShow([heart], N500, 4, null)).toThrow(MorphRefusalError);
    expect(() => buildSequencedShow([heart], N500, 4)).toThrow(MorphRefusalError);
  }, 60000);

  it('with no assignment, every shape either refuses or exports >= 2.0 m and <= 120 m (n=100)', () => {
    for (const entry of SHAPE_LIBRARY) {
      let show;
      try { show = buildSequencedShow([one(entry)], 100, 4, null); } catch (e) {
        expect(e, entry.shapePayload).toBeInstanceOf(MorphRefusalError);
        continue;
      }
      const geo = measureExportedGeometry(show.drones);
      expect(geo.min, entry.shapePayload).toBeGreaterThanOrEqual(2.0 - 1e-9);
      expect(geo.maxAltitude, entry.shapePayload).toBeLessThanOrEqual(120);
      expect(show._minSeparationM).toBeCloseTo(geo.min, 9);
    }
  }, 120000);

  it('the exported-geometry measurement catches a violation the planner never saw', () => {
    const wp = (x, z) => ({ time: 0, x, y: 0, z, yaw: 0 });
    const bad = [
      { id: 0, waypoints: [wp(0, -30), wp(0, -30)] },
      { id: 1, waypoints: [wp(1.99, -30), wp(1.99, -30)] },
    ];
    const geo = measureExportedGeometry(bad);
    expect(geo.min).toBeCloseTo(1.99, 9);
    expect([geo.i, geo.j]).toEqual([0, 1]);
    const high = [{ id: 0, waypoints: [wp(0, -121.5)] }];
    expect(measureExportedGeometry(high).maxAltitude).toBeCloseTo(121.5, 9);
  });

  it('the 120 m ceiling and 2.0 m separation limits are unchanged', () => {
    // Behavioural pin: a 500-drone heart morph needing layers above 120 m is refused with that limit named.
    try { computeShowDuration([heart], N500, 4, null); } catch (e) {
      expect(e.message).toContain('120m ceiling');
      return;
    }
    throw new Error('expected a ceiling refusal');
  });
});
