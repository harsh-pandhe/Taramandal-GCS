import { describe, it, expect } from 'vitest';
import {
  planBeatSnap, buildSnapBatch, runBeatSnap, beatTimes, snapWindow, MAX_SNAP_WINDOW_S,
} from './beatSnap.js';
import { makeScene, buildTimelineSegments } from './sequencer.js';
import { applyCommand } from './commands.js';
import { createHistory, execute, undo, canUndo } from './history.js';

const scene = (id, hold, transition = 4) => makeScene({
  id, name: id, holdDurationSeconds: hold, transitionDurationSeconds: transition,
});
const holdEnd = (segments, id) => segments.find((s) => s.sceneId === id && s.kind === 'hold').end;
const grid = (start, step, count) => Array.from({ length: count }, (_, i) => Math.round((start + i * step) * 1000) / 1000);
const asObjects = (times) => times.map((t) => ({ t, confidence: 0.5 }));

// Three scenes laid out by the real timeline code (takeoff and morphs included).
const threeScenes = () => [scene('a', 6), scene('b', 6), scene('c', 6)];
const segmentsOf = (scenes) => buildTimelineSegments(scenes);

describe('beat helpers', () => {
  it('reads {t} objects and plain numbers, drops junk, and sorts', () => {
    expect(beatTimes([{ t: 2 }, 1, null, { t: NaN }, 'x', { t: 0.5 }, NaN])).toEqual([0.5, 1, 2]);
    expect(beatTimes(undefined)).toEqual([]);
  });
});

describe('snap window', () => {
  it('is capped at 0.75 s for slow tempos', () => {
    expect(snapWindow(grid(0, 2, 6))).toBe(MAX_SNAP_WINDOW_S);   // 30 BPM: half interval = 1.0
  });
  it('shrinks to half the median interval for a fast tempo', () => {
    expect(snapWindow(grid(0, 0.3, 20))).toBeCloseTo(0.15, 6);   // 200 BPM
  });
  it('is 0.75 s when there is no interval to measure', () => {
    expect(snapWindow([])).toBe(MAX_SNAP_WINDOW_S);
    expect(snapWindow([3])).toBe(MAX_SNAP_WINDOW_S);
  });
});

describe('planBeatSnap', () => {
  it('snaps each scene end to the nearest beat by changing only the hold (object beats)', () => {
    const scenes = threeScenes();
    const segments = segmentsOf(scenes);
    const end = holdEnd(segments, 'a');
    // A beat 0.4 s after the end of scene a, nothing near b or c.
    const plan = planBeatSnap({ scenes: [scenes[0]], segments, beats: asObjects([end + 0.4, end + 20]) });
    expect(plan.patches).toEqual([{ id: 'a', holdDurationSeconds: 6.4 }]);
    expect(plan.aligned).toBe(0);
  });

  it('accepts plain-number beats', () => {
    const scenes = threeScenes();
    const segments = segmentsOf(scenes);
    const end = holdEnd(segments, 'a');
    const plan = planBeatSnap({ scenes: [scenes[0]], segments, beats: [end - 0.5, end + 30] });
    expect(plan.patches).toEqual([{ id: 'a', holdDurationSeconds: 5.5 }]);
  });

  it('picks the nearer of two beats', () => {
    const scenes = [scene('a', 6)];
    const segments = segmentsOf(scenes);
    const end = holdEnd(segments, 'a');
    const plan = planBeatSnap({ scenes, segments, beats: [end - 0.6, end + 0.2, end + 9] });
    expect(plan.patches).toEqual([{ id: 'a', holdDurationSeconds: 6.2 }]);
  });

  it('carries the shift of an earlier scene to every later one', () => {
    const scenes = threeScenes();
    const segments = segmentsOf(scenes);
    const endA = holdEnd(segments, 'a');
    const endB = holdEnd(segments, 'b');
    // a's hold grows by 0.5, which pushes b's end 0.5 s later: its raw end is endB + 0.5,
    // so the beat at endB + 0.2 is 0.3 s back, not 0.2 s ahead (6.2) as it would be unshifted.
    const beats = [endA + 0.5, endB + 0.2, 9999];
    const plan = planBeatSnap({ scenes: scenes.slice(0, 2), segments, beats });
    expect(plan.patches).toEqual([
      { id: 'a', holdDurationSeconds: 6.5 },
      { id: 'b', holdDurationSeconds: 5.7 },
    ]);
  });

  it('a scene already on a beat is counted as aligned, not patched', () => {
    const scenes = [scene('a', 6)];
    const segments = segmentsOf(scenes);
    const plan = planBeatSnap({ scenes, segments, beats: [holdEnd(segments, 'a')] });
    expect(plan).toEqual({ patches: [], aligned: 1, skipped: [] });
  });

  it('skips a scene whose nearest beat is outside the window', () => {
    const scenes = [scene('a', 6)];
    const segments = segmentsOf(scenes);
    const end = holdEnd(segments, 'a');
    const plan = planBeatSnap({ scenes, segments, beats: [end + 0.8, end + 5] });
    expect(plan.patches).toEqual([]);
    expect(plan.skipped).toEqual([{ id: 'a', reason: 'no beat within the snap window' }]);
  });

  it('the window shrinks with tempo: a beat 0.3 s away is skipped at 200 BPM, taken at 60 BPM', () => {
    const scenes = [scene('a', 6)];
    const segments = segmentsOf(scenes);
    const end = holdEnd(segments, 'a');
    // One stray beat 0.3 s after the scene end; the rest of the grid is far away.
    const fast = [end + 0.3, ...grid(end + 100, 0.3, 50)];   // median gap 0.3 -> window 0.15
    expect(planBeatSnap({ scenes, segments, beats: fast }).skipped).toHaveLength(1);
    const slow = [end + 0.3, ...grid(end + 100, 1, 50)];     // median gap 1 -> window 0.5
    expect(planBeatSnap({ scenes, segments, beats: slow }).patches).toEqual([{ id: 'a', holdDurationSeconds: 6.3 }]);
  });

  it('skips a scene whose snapped hold would be under 1 s', () => {
    const scenes = [scene('a', 1.2)];
    const segments = segmentsOf(scenes);
    const end = holdEnd(segments, 'a');
    const plan = planBeatSnap({ scenes, segments, beats: [end - 0.5, end + 40] });
    expect(plan.patches).toEqual([]);
    expect(plan.skipped[0].id).toBe('a');
    expect(plan.skipped[0].reason).toMatch(/under 1 s/);
  });

  it('skips a scene whose snapped hold would leave SCENE_LIMITS, and does not shift later scenes for it', () => {
    const scenes = [scene('a', 119.9), scene('b', 6)];
    const segments = segmentsOf(scenes);
    const endA = holdEnd(segments, 'a');
    const endB = holdEnd(segments, 'b');
    const plan = planBeatSnap({ scenes, segments, beats: [endA + 0.5, endB + 0.25, endB + 99] });
    expect(plan.skipped).toEqual([expect.objectContaining({ id: 'a', reason: expect.stringMatching(/outside 0 to 120/) })]);
    expect(plan.patches).toEqual([{ id: 'b', holdDurationSeconds: 6.25 }]);
  });

  it('the first scene uses its real segment end, takeoff included', () => {
    const scenes = [scene('a', 6, 0)];
    const segments = segmentsOf(scenes);
    const end = holdEnd(segments, 'a');
    expect(end).toBeGreaterThan(6); // takeoff comes first; a cumulative sum from 0 would say 6
    const plan = planBeatSnap({ scenes, segments, beats: [end + 0.3, end + 50] });
    expect(plan.patches).toEqual([{ id: 'a', holdDurationSeconds: 6.3 }]);
    // Beats placed where a from-zero sum would put the end (6 s) must not pull it there.
    const naive = planBeatSnap({ scenes, segments, beats: [6, 6.1, 6.2] });
    expect(naive.patches).toEqual([]);
  });

  it('does nothing and does not throw on empty beats, NaN, or junk input', () => {
    const scenes = threeScenes();
    const segments = segmentsOf(scenes);
    expect(planBeatSnap({ scenes, segments, beats: [] }).patches).toEqual([]);
    expect(planBeatSnap({ scenes, segments, beats: [NaN, null, { t: NaN }] }).patches).toEqual([]);
    expect(planBeatSnap({ scenes, segments, beats: undefined }).patches).toEqual([]);
    expect(planBeatSnap({ scenes, segments: undefined, beats: [1, 2] }).patches).toEqual([]);
    expect(planBeatSnap({}).patches).toEqual([]);
    expect(planBeatSnap().patches).toEqual([]);
    expect(planBeatSnap({ scenes: [{ id: 'x' }], segments: [], beats: [1] }).skipped)
      .toEqual([{ id: 'x', reason: 'no timeline position' }]);
  });

  it('a scene without a timeline segment is skipped; the others still snap', () => {
    const scenes = [scene('a', 6), scene('b', 6)];
    const segments = segmentsOf(scenes).filter((s) => s.sceneId !== 'a');
    const end = holdEnd(segments, 'b');
    const plan = planBeatSnap({ scenes, segments, beats: [end + 0.3, end + 80] });
    expect(plan.skipped).toEqual([{ id: 'a', reason: 'no timeline position' }]);
    expect(plan.patches).toEqual([{ id: 'b', holdDurationSeconds: 6.3 }]);
  });

  it('never yields NaN and never touches the old scene.hold / scene.transition names (#274)', () => {
    const scenes = threeScenes();
    for (const s of scenes) {
      expect(s.hold).toBeUndefined();
      expect(s.transition).toBeUndefined();
    }
    const segments = segmentsOf(scenes);
    const beats = grid(holdEnd(segments, 'a') + 0.3, 0.9, 40);
    const plan = planBeatSnap({ scenes, segments, beats });
    expect(plan.patches.length).toBeGreaterThan(0);
    for (const p of plan.patches) {
      expect(Object.keys(p).sort()).toEqual(['holdDurationSeconds', 'id']);
      expect(Number.isFinite(p.holdDurationSeconds)).toBe(true);
    }
    expect(JSON.stringify(plan)).not.toMatch(/null|NaN/);
  });
});

describe('snap as one undoable batch', () => {
  const docOf = () => ({ scenes: threeScenes(), dronesList: [] });
  const planFor = (doc) => {
    const segments = segmentsOf(doc.scenes);
    const beats = asObjects(grid(holdEnd(segments, 'a') + 0.3, 0.9, 40));
    return { segments, beats, analysis: { beats } };
  };

  it('changes only holdDurationSeconds, leaves no `hold` key, and one undo restores the document exactly', () => {
    const doc = docOf();
    const { segments, analysis } = planFor(doc);
    const { command, message } = runBeatSnap({ doc, segments, analysis });
    expect(command.type).toBe('scene/batch');
    expect(command.label).toBe('Snap scenes to beats');
    expect(message).toMatch(/^Snapped \d+ scenes?, \d+ already on a beat, \d+ skipped$/);

    let history = createHistory(doc);
    history = execute(history, applyCommand, command, 0);
    const after = history.present;
    expect(after).not.toBe(doc);
    expect(after.scenes.some((s, i) => s.holdDurationSeconds !== doc.scenes[i].holdDurationSeconds)).toBe(true);
    for (const [i, s] of after.scenes.entries()) {
      expect('hold' in s).toBe(false);
      expect('transition' in s).toBe(false);
      expect(s.transitionDurationSeconds).toBe(doc.scenes[i].transitionDurationSeconds);
      expect(Number.isFinite(s.holdDurationSeconds)).toBe(true);
    }
    expect(history.past).toHaveLength(1);          // one entry = one undo step

    history = undo(history);
    expect(history.present).toEqual(doc);
    expect(history.present).toBe(doc);             // the very same document, not a lookalike
    expect(canUndo(history)).toBe(false);
  });

  it('the resulting scene ends land on the beats they snapped to', () => {
    const doc = docOf();
    const { segments, analysis } = planFor(doc);
    const { command } = runBeatSnap({ doc, segments, analysis });
    const after = applyCommand(doc, command);
    const newSegments = segmentsOf(after.scenes);
    const times = beatTimes(analysis.beats);
    for (const s of after.scenes) {
      if (s.holdDurationSeconds === doc.scenes.find((d) => d.id === s.id).holdDurationSeconds) continue;
      const end = holdEnd(newSegments, s.id);
      expect(Math.min(...times.map((b) => Math.abs(b - end)))).toBeLessThan(0.011);
    }
  });

  it('is refused whole when the document changed after it was planned', () => {
    const doc = docOf();
    const { segments, analysis } = planFor(doc);
    const { command } = runBeatSnap({ doc, segments, analysis });
    const edited = applyCommand(doc, { type: 'scene/patch', id: 'a', patch: { name: 'renamed' } });
    expect(applyCommand(edited, command)).toBe(edited);
  });

  it('buildSnapBatch returns null when there is nothing to change', () => {
    const doc = docOf();
    expect(buildSnapBatch(doc, { patches: [], aligned: 3, skipped: [] })).toBeNull();
    expect(buildSnapBatch(doc, undefined)).toBeNull();
  });
});

// The real property: build the segments the way the app does (sample rate, and a fleet
// size for the planned path), apply the snap, rebuild them, and check where the ends are.
describe.each([
  { label: 'no fleet, 10 Hz', n: null, rate: 10 },
  { label: 'no fleet, 30 Hz', n: null, rate: 30 },
  { label: 'no fleet, no rate', n: null, rate: null },
  { label: '8 drones, 10 Hz', n: 8, rate: 10 },
])('scene ends land on the beats after rebuilding the timeline ($label)', ({ n, rate }) => {
  // Holds that are not whole samples, so rounding by the timeline is exercised.
  const scenes = () => [scene('a', 6.03), scene('b', 7.21), scene('c', 5.5), scene('d', 8.07), scene('e', 6.66)];
  const build = (list) => buildTimelineSegments(list, n, rate, null, 'standard');
  const beatGrid = (offset) => grid(offset, 0.5, 200);
  const tolerance = (rate ? 0.5 / rate : 0.005) + 0.006; // half a sample, plus the timeline's 2-decimal rounding
  const distToGrid = (t, times) => Math.min(...times.map((b) => Math.abs(b - t)));

  it.each([0.13, 0.31, 0.44])('every snapped end is on a beat, later scenes included (grid offset %s)', (offset) => {
    const doc = { scenes: scenes(), dronesList: [] };
    const times = beatGrid(offset);
    const { command, message } = runBeatSnap({ doc, segments: build(doc.scenes), analysis: { beats: times }, rate });
    expect(command).not.toBeNull();
    expect(message).toMatch(/^Snapped [1-5] scenes?/);
    const after = applyCommand(doc, command);
    const newSegments = build(after.scenes);
    const patched = after.scenes.filter((s, i) => s.holdDurationSeconds !== doc.scenes[i].holdDurationSeconds);
    expect(patched.length).toBeGreaterThan(1); // an earlier scene shifts the later ones
    for (const s of patched) {
      expect(distToGrid(holdEnd(newSegments, s.id), times)).toBeLessThanOrEqual(tolerance);
    }
  });

  it('a second snap of the result changes nothing', () => {
    const doc = { scenes: scenes(), dronesList: [] };
    const times = beatGrid(0.13);
    const first = runBeatSnap({ doc, segments: build(doc.scenes), analysis: { beats: times }, rate });
    const after = applyCommand(doc, first.command);
    const again = runBeatSnap({ doc: after, segments: build(after.scenes), analysis: { beats: times }, rate });
    expect(again.command).toBeNull();
    expect(again.message).toMatch(/^Snapped 0 scenes/);
  });
});

describe('odd input', () => {
  const doc = () => ({ scenes: threeScenes(), dronesList: [] });
  const base = () => {
    const d = doc();
    const segments = segmentsOf(d.scenes);
    return { d, segments, end: holdEnd(segments, 'a') };
  };

  it('beat order and duplicates make no difference', () => {
    const { d, segments, end } = base();
    const sorted = [end - 0.2, end + 40];
    const messy = [end + 40, end - 0.2, end - 0.2, { t: end + 40 }, end + 40];
    const a = planBeatSnap({ scenes: d.scenes.slice(0, 1), segments, beats: sorted });
    const b = planBeatSnap({ scenes: d.scenes.slice(0, 1), segments, beats: messy });
    expect(b).toEqual(a);
    expect(a.patches).toEqual([{ id: 'a', holdDurationSeconds: 5.8 }]);
  });

  it('a single beat uses the 0.75 s window', () => {
    const { d, segments, end } = base();
    expect(planBeatSnap({ scenes: d.scenes.slice(0, 1), segments, beats: [end + 0.7] }).patches).toHaveLength(1);
    expect(planBeatSnap({ scenes: d.scenes.slice(0, 1), segments, beats: [end + 0.8] }).patches).toHaveLength(0);
  });

  it('beats outside the show (before the start, after the end) skip every scene', () => {
    const { d, segments } = base();
    const plan = planBeatSnap({ scenes: d.scenes, segments, beats: [-50, -49, 5000, 5001] });
    expect(plan.patches).toEqual([]);
    expect(plan.skipped).toHaveLength(3);
  });

  it('a rate of 0, NaN, a string or undefined is treated as no rate', () => {
    const { d, segments, end } = base();
    const want = planBeatSnap({ scenes: d.scenes.slice(0, 1), segments, beats: [end + 0.3, end + 60] });
    for (const rate of [0, NaN, 'fast', undefined, null, -5, 0.5]) {
      expect(planBeatSnap({ scenes: d.scenes.slice(0, 1), segments, beats: [end + 0.3, end + 60], rate })).toEqual(want);
    }
  });

  it('a batch planned for an older document is refused, not applied', () => {
    const { d, segments } = base();
    const { command } = runBeatSnap({ doc: d, segments, analysis: { beats: grid(holdEnd(segments, 'a') + 0.3, 1, 60) } });
    // renamed, hold edited, a scene added or removed, scenes reordered: all pinned out
    const renamed = applyCommand(d, { type: 'scene/patch', id: 'a', patch: { name: 'x' } });
    const edited = applyCommand(d, { type: 'scene/patch', id: 'b', patch: { holdDurationSeconds: 9 } });
    const added = applyCommand(d, { type: 'scene/add', scene: scene('z', 5) });
    const removed = applyCommand(d, { type: 'scene/delete', id: 'c' });
    const moved = applyCommand(d, { type: 'scene/move', index: 0, dir: 1 });
    for (const changed of [renamed, edited, added, removed, moved]) {
      expect(changed).not.toBe(d);
      expect(applyCommand(changed, command)).toBe(changed);
    }
  });

  it('clicking twice before the document updates records one step (the second batch is stale)', () => {
    const { d, segments } = base();
    const analysis = { beats: grid(holdEnd(segments, 'a') + 0.3, 1, 60) };
    const { command } = runBeatSnap({ doc: d, segments, analysis });
    let h = createHistory(d);
    h = execute(h, applyCommand, command, 0);
    h = execute(h, applyCommand, command, 1);
    expect(h.past).toHaveLength(1);
  });

  it('a scene with no segment, or a non-finite hold, is skipped and never produces NaN', () => {
    const { d, segments, end } = base();
    const broken = [{ ...d.scenes[0], holdDurationSeconds: NaN }, d.scenes[1]];
    const plan = planBeatSnap({ scenes: broken, segments, beats: [end + 0.3, holdEnd(segments, 'b') + 0.3] });
    expect(plan.skipped.map((s) => s.id)).toEqual(['a']);
    expect(JSON.stringify(plan)).not.toMatch(/NaN|null/);
  });
});

describe('runBeatSnap messages', () => {
  const doc = { scenes: threeScenes(), dronesList: [] };
  const segments = segmentsOf(doc.scenes);

  it('says so when there is no tempo or no beats, and changes nothing', () => {
    for (const analysis of [null, undefined, { bpm: null, beats: [] }, {}]) {
      const r = runBeatSnap({ doc, segments, analysis });
      expect(r.command).toBeNull();
      expect(r.message).toMatch(/No tempo or beats/);
    }
  });

  it('says so when the timeline is not available', () => {
    const r = runBeatSnap({ doc, segments: [], analysis: { beats: [1, 2, 3] } });
    expect(r.command).toBeNull();
    expect(r.message).toMatch(/timeline is not available/);
  });

  it('reports counts when nothing needs to change', () => {
    const beats = doc.scenes.map((s) => holdEnd(segments, s.id));
    const r = runBeatSnap({ doc, segments, analysis: { beats } });
    expect(r.command).toBeNull();
    expect(r.message).toBe('Snapped 0 scenes, 3 already on a beat, 0 skipped');
  });
});
