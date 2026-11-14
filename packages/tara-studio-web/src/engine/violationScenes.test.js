import { describe, expect, it } from 'vitest';
import { classify, explain, sceneAtTime, violationsByScene, violationsStale } from './violationScenes.js';
import { sceneSignature } from './sceneValidation.js';
import { buildTimelineSegments, makeScene } from './sequencer.js';

// takeoff 0-5 | climb-out A 5-9 | hold A 9-15 | morph B 15-19 | hold B 19-25 | return 25-29 | land 29-34
const SEGS = [
  { kind: 'takeoff', label: 'Takeoff', sceneId: null, start: 0, end: 5 },
  { kind: 'morph', label: 'Climb-out', sceneId: 'a', start: 5, end: 9 },
  { kind: 'hold', label: 'Five', sceneId: 'a', start: 9, end: 15 },
  { kind: 'morph', label: 'Morph → Four', sceneId: 'b', start: 15, end: 19 },
  { kind: 'hold', label: 'Four', sceneId: 'b', start: 19, end: 25 },
  { kind: 'morph', label: 'Return', sceneId: 'b', start: 25, end: 29 },
  { kind: 'land', label: 'Landing', sceneId: null, start: 29, end: 34 },
];
const SCENES = [{ id: 'a', name: 'Five' }, { id: 'b', name: 'Four' }];
const run = (violations, over = {}) => violationsByScene({ violations, segments: SEGS, scenes: SCENES, stale: false, ...over });

describe('sceneAtTime', () => {
  it('is half-open: a boundary belongs to the segment that starts there', () => {
    expect(sceneAtTime(SEGS, 9).kind).toBe('hold');
    expect(sceneAtTime(SEGS, 8.99).kind).toBe('morph');
    expect(sceneAtTime(SEGS, 0).kind).toBe('takeoff');
    expect(sceneAtTime(SEGS, 15).label).toBe('Morph → Four');
  });
  it('uses the last segment at or past the show duration', () => {
    expect(sceneAtTime(SEGS, 34).kind).toBe('land');
    expect(sceneAtTime(SEGS, 99).kind).toBe('land');
  });
  it('returns null, never throws, on odd input', () => {
    expect(sceneAtTime([], 3)).toBeNull();
    expect(sceneAtTime(null, 3)).toBeNull();
    expect(sceneAtTime(undefined, 3)).toBeNull();
    expect(sceneAtTime(SEGS, NaN)).toBeNull();
    expect(sceneAtTime(SEGS, '6')).toBeNull();
    expect(sceneAtTime(SEGS, -1)).toBeNull();
    expect(sceneAtTime([null, {}], 3)).toBeNull();
  });
});

describe('classify', () => {
  const kinds = SEGS.map((s) => classify(s, SEGS));
  it('tells holds, morphs-in, the return, takeoff and landing apart', () => {
    expect(kinds).toEqual(['takeoff', 'transition', 'hold', 'transition', 'hold', 'return', 'landing']);
  });
  it('finds the return by position, not by its label', () => {
    const renamed = SEGS.map((s) => (s.label === 'Return' ? { ...s, label: 'Back to pads' } : s));
    expect(classify(renamed[5], renamed)).toBe('return');
    const sneaky = SEGS.map((s) => (s.label === 'Morph → Four' ? { ...s, label: 'Return' } : s));
    expect(classify(sneaky[3], sneaky)).toBe('transition');
  });
  it('still finds the return when the last scene has no hold segment (0 s hold)', () => {
    const noHold = SEGS.filter((s) => !(s.kind === 'hold' && s.sceneId === 'b'));
    expect(classify(noHold[4], noHold)).toBe('return');
  });
  it('matches the real timeline builder', () => {
    const scenes = [makeScene({ id: 's1', name: 'One' }), makeScene({ id: 's2', name: 'Two' })];
    const segs = buildTimelineSegments(scenes);
    expect(segs.map((s) => classify(s, segs))).toEqual(
      ['takeoff', 'transition', 'hold', 'transition', 'hold', 'return', 'landing'],
    );
  });
  it('is null for junk', () => {
    expect(classify(null, SEGS)).toBeNull();
    expect(classify({ kind: 'weird' }, SEGS)).toBeNull();
  });
});

describe('explain', () => {
  it('proximity names both drones, with limit and time', () => {
    expect(explain('proximity', { time: 6.1, drones: [2, 3], distance: 1.2, limit: 2 }))
      .toBe('Drones 2 and 3 came 1.2 m apart (limit 2.0 m) at 6.1 s');
  });
  it('every other category uses a single drone', () => {
    expect(explain('velocity', { time: 12.4, drone: 4, velocity: 4.8, limit: 4 }))
      .toBe('Drone 4 reached 4.8 m/s (limit 4.0 m/s) at 12.4 s');
    expect(explain('acceleration', { time: 3, drone: 1, acceleration: 55.5, limit: 50 }))
      .toBe('Drone 1 accelerated at 55.5 m/s² (limit 50.0 m/s²) at 3.0 s');
    expect(explain('yaw_rate', { time: 3, drone: 1, yaw_rate: 320, limit: 300 }))
      .toBe('Drone 1 turned at 320.0 °/s (limit 300.0 °/s) at 3.0 s');
    expect(explain('jerk', { time: 3, drone: 0, jerk: 21.5, limit: 20 }))
      .toBe('Drone 0 changed acceleration at 21.5 m/s³ (limit 20.0 m/s³) at 3.0 s');
    expect(explain('altitude', { time: 8, drone: 2, altitude: 130, limit: 120 }))
      .toBe('Drone 2 climbed to 130.0 m (limit 120.0 m) at 8.0 s');
    expect(explain('ground', { time: 8, drone: 2, altitude: -0.5, limit: 0 }))
      .toBe('Drone 2 went 0.5 m below ground level at 8.0 s');
  });
  it('drone 0 is a real drone, not a missing one', () => {
    expect(explain('velocity', { time: 1, drone: 0, velocity: 7 })).toContain('Drone 0');
  });
  it('tolerates a missing limit, positions, time and drones', () => {
    expect(explain('proximity', { time: 6.1, drones: [2, 3], distance: 1.2 }))
      .toBe('Drones 2 and 3 came 1.2 m apart at 6.1 s');
    expect(explain('velocity', { drone: 4, velocity: 4.8 })).toBe('Drone 4 reached 4.8 m/s');
    expect(explain('proximity', { time: 1 })).toBe('Two drones came too close at 1.0 s');
    expect(explain('jerk', {})).toBe('A drone changed acceleration too sharply');
    expect(explain('ground', null)).toBe('A drone went below ground level');
    expect(explain('mystery', { time: 2 })).toBe('Safety issue (mystery) at 2.0 s');
    expect(explain(undefined, undefined)).toBe('Safety issue (undefined)');
  });
});

describe('violationsByScene', () => {
  it('groups by scene in scene order, items by time, counts per category', () => {
    const r = run({
      velocity: [{ time: 20, drone: 4, velocity: 7, limit: 6 }],
      proximity: [
        { time: 16, drones: [2, 3], distance: 1.2 },
        { time: 10, drones: [2, 3], distance: 1.3 },
        { time: 12, drones: [2, 3], distance: 1.4 },
      ],
    });
    expect(r.stale).toBe(false);
    expect(r.groups.map((g) => g.sceneId)).toEqual(['a', 'b']);
    const [a, b] = r.groups;
    expect(a).toMatchObject({ name: 'Five', index: 0, firstTime: 10, counts: { proximity: 2 } });
    expect(a.items.map((i) => i.time)).toEqual([10, 12]);
    expect(a.items.every((i) => i.where === 'hold')).toBe(true);
    expect(b).toMatchObject({ name: 'Four', index: 1, firstTime: 16, counts: { proximity: 1, velocity: 1 } });
    expect(b.items.map((i) => [i.category, i.where])).toEqual([['proximity', 'transition'], ['velocity', 'hold']]);
    expect(b.items[0].text).toContain('Drones 2 and 3');
    expect(r.unassigned).toEqual([]);
  });
  it('orders groups by scene order even when a later scene fails first in the report', () => {
    const r = run({ velocity: [{ time: 20, drone: 1, velocity: 9 }], jerk: [{ time: 10, drone: 1, jerk: 30 }] });
    expect(r.groups.map((g) => g.sceneId)).toEqual(['a', 'b']);
  });
  it('sends takeoff and landing to unassigned, including the closing sample', () => {
    const r = run({ ground: [{ time: 2, drone: 0, altitude: -1 }], velocity: [{ time: 34, drone: 0, velocity: 9 }, { time: 30, drone: 1, velocity: 9 }] });
    expect(r.groups).toEqual([]);
    expect(r.unassigned.map((i) => [i.category, i.time, i.where])).toEqual([
      ['ground', 2, 'takeoff'], ['velocity', 30, 'landing'], ['velocity', 34, 'landing'],
    ]);
  });
  it('attributes the return leg to the last scene as a return, even if its label changes', () => {
    const segs = SEGS.map((s) => (s.label === 'Return' ? { ...s, label: 'Back to pads' } : s));
    const r = run({ proximity: [{ time: 26, drones: [0, 1], distance: 1 }] }, { segments: segs });
    expect(r.groups).toHaveLength(1);
    expect(r.groups[0]).toMatchObject({ sceneId: 'b', name: 'Four' });
    expect(r.groups[0].items[0].where).toBe('return');
  });
  it('puts a violation whose scene is not in the scene list in unassigned', () => {
    const r = run({ velocity: [{ time: 10, drone: 1, velocity: 9 }] }, { scenes: [{ id: 'b', name: 'Four' }] });
    expect(r.groups).toEqual([]);
    expect(r.unassigned).toHaveLength(1);
  });
  it('puts a violation before the timeline in unassigned as unknown', () => {
    const r = run({ velocity: [{ time: -1, drone: 1, velocity: 9 }] });
    expect(r.unassigned[0].where).toBe('unknown');
  });
  it('returns no groups for empty segments', () => {
    expect(run({ velocity: [{ time: 1, drone: 1, velocity: 9 }] }, { segments: [] }))
      .toEqual({ stale: false, groups: [], unassigned: [] });
  });
  it('returns only the stale flag when stale', () => {
    expect(run({ velocity: [{ time: 10, drone: 1, velocity: 9 }] }, { stale: true }))
      .toEqual({ stale: true, groups: [], unassigned: [] });
  });
  it('never throws on odd input', () => {
    expect(() => violationsByScene()).not.toThrow();
    expect(() => violationsByScene({ violations: 5, segments: SEGS, scenes: null })).not.toThrow();
    const r = run({ velocity: 'x', proximity: [null, 3, { drones: [1, 2] }, { time: 'a' }], jerk: [{ time: 10, drone: 1, jerk: 25 }] }, { scenes: undefined });
    expect(r.groups).toEqual([]);
    expect(r.unassigned).toHaveLength(1);
  });
  it('works on the e2e-style thin report (no limit, no positions)', () => {
    const r = run({
      proximity: [{ time: 10.1, drones: [2, 3], distance: 1.2 }, { time: 10.2, drones: [2, 3], distance: 1.25 }],
      velocity: [{ time: 20.4, drone: 4, velocity: 4.8 }],
      acceleration: [],
    });
    expect(r.groups.map((g) => g.counts)).toEqual([{ proximity: 2 }, { velocity: 1 }]);
  });
});

describe('violationsStale (content-based)', () => {
  const doc = () => ({
    scenes: [
      makeScene({ id: 'a', name: 'Five', shapeType: 'text', shapePayload: '5', holdDurationSeconds: 6, transitionDurationSeconds: 0 }),
      makeScene({ id: 'b', name: 'Four', shapeType: 'text', shapePayload: '4', holdDurationSeconds: 6, transitionDurationSeconds: 4 }),
    ],
  });
  const recorded = sceneSignature(doc());

  it('an unchanged doc is not stale, even as a fresh copy', () => {
    expect(violationsStale(recorded, doc())).toBe(false);
  });
  it('a changed shape, name, scale or layer gap with the SAME total duration is stale', () => {
    const edit = (patch) => { const d = doc(); d.scenes[0] = { ...d.scenes[0], ...patch }; return d; };
    expect(violationsStale(recorded, edit({ shapePayload: '7' }))).toBe(true);
    expect(violationsStale(recorded, edit({ shapeType: 'parametric', shapePayload: 'circle' }))).toBe(true);
    expect(violationsStale(recorded, edit({ name: 'Renamed' }))).toBe(true);
    expect(violationsStale(recorded, edit({ scale: 1.5 }))).toBe(true);
    expect(violationsStale(recorded, edit({ transitLayerGapMeters: 9 }))).toBe(true);
  });
  it('trading hold time between scenes (same total) is stale', () => {
    const d = doc();
    d.scenes[0].holdDurationSeconds = 8;
    d.scenes[1].holdDurationSeconds = 4;
    expect(violationsStale(recorded, d)).toBe(true);
  });
  it('reordering, adding and removing scenes is stale', () => {
    const d = doc();
    expect(violationsStale(recorded, { scenes: [...d.scenes].reverse() })).toBe(true);
    expect(violationsStale(recorded, { scenes: d.scenes.slice(0, 1) })).toBe(true);
    expect(violationsStale(recorded, { scenes: [...d.scenes, makeScene()] })).toBe(true);
  });
  it('a result with no recorded signature is stale', () => {
    expect(violationsStale(null, doc())).toBe(true);
    expect(violationsStale(undefined, doc())).toBe(true);
    expect(violationsStale(42, doc())).toBe(true);
  });
  it('never throws on odd docs', () => {
    expect(() => violationsStale(recorded, null)).not.toThrow();
    expect(violationsStale(recorded, undefined)).toBe(true);
  });
});
