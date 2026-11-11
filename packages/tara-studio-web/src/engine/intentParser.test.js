import { describe, it, expect } from 'vitest';
import { parseIntent } from './intentParser.js';
import { applyCommand } from './commands.js';
import { createHistory, execute, undo, timeline } from './history.js';
import { makeScene } from './sequencer.js';
import { sceneSignature } from './sceneValidation.js';
import { SHAPE_LIBRARY } from './formations.js';
import { defaultFleet } from './fleetCommands.js';

const mkDoc = () => ({
  scenes: ['a', 'b', 'c', 'd'].map((id, i) => makeScene({ id, name: `S${i + 1}` })),
  dronesList: defaultFleet(3),
});
const ids = (doc) => doc.scenes.map((s) => s.id);

/** Parses and applies; returns { parsed, out }. */
const run = (text, doc = mkDoc()) => {
  const parsed = parseIntent(text, doc);
  expect(parsed.ok, `${text}: ${parsed.reason}`).toBe(true);
  const out = applyCommand(doc, parsed.command);
  expect(out).not.toBe(doc);
  return { parsed, out, doc };
};

describe('parseIntent: grammar', () => {
  it('make scene N a <shape>', () => {
    const { parsed, out } = run('Make scene 2 a Heart');
    expect(parsed.command.type).toBe('scene/batch');
    expect(parsed.command.expectIds).toEqual(['a', 'b', 'c', 'd']);
    expect(parsed.command.mergeKey).toBeUndefined();
    expect(out.scenes[1]).toMatchObject({ shapeType: 'parametric', shapePayload: 'heart' });
    expect(parsed.summary).toMatch(/heart/i);
  });

  it('accepts every library label and parametric name', () => {
    for (const e of SHAPE_LIBRARY) {
      // Start scene 1 on a different shape: setting a scene to the shape it
      // already has is (deliberately) refused as a no-op.
      const doc = mkDoc();
      doc.scenes[0] = { ...doc.scenes[0], shapeType: 'parametric', shapePayload: e.shapePayload === 'sphere' ? 'heart' : 'sphere' };
      const { out } = run(`make scene 1 a ${e.label.toUpperCase()}`, doc);
      expect(out.scenes[0]).toMatchObject({ shapeType: e.shapeType, shapePayload: e.shapePayload });
    }
    const heartFirst = mkDoc();
    heartFirst.scenes[0] = { ...heartFirst.scenes[0], shapePayload: 'heart' };
    expect(run('make scene 1 a circle', heartFirst).out.scenes[0].shapePayload).toBe('circle');
    expect(run('make scene 3 a sphere').out.scenes[2].shapePayload).toBe('sphere');
    expect(run('make scene 1 a digit 8').out.scenes[0]).toMatchObject({ shapeType: 'text', shapePayload: '8' });
  });

  it('set hold / transition', () => {
    expect(run('set hold of scene 1 to 12 s').out.scenes[0].holdDurationSeconds).toBe(12);
    expect(run('SET HOLD OF SCENE 3 TO 7.5 seconds').out.scenes[2].holdDurationSeconds).toBe(7.5);
    expect(run('set transition of scene 4 to 3s').out.scenes[3].transitionDurationSeconds).toBe(3);
    expect(run('set transition of scene 2 to 0 s').out.scenes[1].transitionDurationSeconds).toBe(0);
  });

  it('move after / before resolve to the right order', () => {
    expect(ids(run('move scene 1 after scene 3').out)).toEqual(['b', 'c', 'a', 'd']);
    expect(ids(run('move scene 3 after scene 1').out)).toEqual(['a', 'c', 'b', 'd']);
    expect(ids(run('move scene 1 before scene 3').out)).toEqual(['b', 'a', 'c', 'd']);
    expect(ids(run('move scene 4 before scene 2').out)).toEqual(['a', 'd', 'b', 'c']);
    expect(ids(run('move scene 4 after scene 1').out)).toEqual(['a', 'd', 'b', 'c']);
    expect(ids(run('move scene 1 after scene 4').out)).toEqual(['b', 'c', 'd', 'a']);
    expect(ids(run('move scene 4 before scene 1').out)).toEqual(['d', 'a', 'b', 'c']);
  });

  it('refuses moves that would change nothing, or relate a scene to itself', () => {
    expect(parseIntent('move scene 1 before scene 2', mkDoc()).ok).toBe(false);
    expect(parseIntent('move scene 2 after scene 1', mkDoc()).ok).toBe(false);
    expect(parseIntent('move scene 2 after scene 2', mkDoc()).ok).toBe(false);
  });

  it('delete / duplicate', () => {
    expect(ids(run('delete scene 2').out)).toEqual(['a', 'c', 'd']);
    const dup = run('duplicate scene 2').out;
    expect(dup.scenes).toHaveLength(5);
    expect(dup.scenes[2].name).toBe('S2 copy');
    expect(dup.scenes[2].id).not.toBe('b');
  });

  it('add scene <shape>', () => {
    const { out } = run('add scene heart');
    expect(out.scenes).toHaveLength(5);
    expect(out.scenes[4]).toMatchObject({ shapePayload: 'heart', name: 'Scene 5' });
    expect(run('add scene digit 3').out.scenes[4]).toMatchObject({ shapeType: 'text', shapePayload: '3' });
  });

  it('is one undo step through the history', () => {
    const doc = mkDoc();
    const { parsed } = run('make scene 2 a heart', doc);
    let h = execute(createHistory(doc), applyCommand, parsed.command, 0);
    expect(timeline(h)).toHaveLength(1);
    h = undo(h);
    expect(h.present).toBe(doc);
  });
});

describe('parseIntent: refusals', () => {
  const refused = [
    '', '   ', 'hello', 'make it pretty',
    'scene 1', 'make scene 1', 'make scene 1 a',
    'make scene 0 a heart', 'make scene -1 a heart', 'make scene 99 a heart', 'make scene 1.5 a heart',
    'make scene two a heart',
    'make scene 2 a dragon',
    'make scene 2 a heart and delete scene 1',
    'make scene 2 a heart, delete scene 1',
    'delete scene 1 then delete scene 2',
    'delete scene 0', 'delete scene 99', 'duplicate scene -1', 'duplicate scene 5',
    'move scene 0 after scene 2', 'move scene 1 after scene 99',
    'add scene dragon', 'add scene', 'add scene 8',
    'set hold of scene 1 to 500 s', 'set hold of scene 1 to -1 s', 'set hold of scene 1 to 121 s',
    'set transition of scene 1 to 61 s', 'set hold of scene 99 to 5 s', 'set hold of scene 1 to abc s',
    'set hold of scene 1 to 1e3 s', 'set hold of scene 1 to 5',
    'set altitude to 200 m', 'lower separation to 1 m', 'export the show', 'transmit now',
    'arm all drones', 'land all drones', 'start the show', 'send it to the gcs', 'upload the trajectory',
    'raise the speed limit', 'change the geofence', 'sign the show', 'set scale limit of scene 1 to 9',
  ];
  for (const text of refused) {
    it(`refuses ${JSON.stringify(text)}`, () => {
      const r = parseIntent(text, mkDoc());
      expect(r.ok).toBe(false);
      expect(typeof r.reason).toBe('string');
      expect(r.reason.length).toBeGreaterThan(0);
    });
  }

  it('says "not supported" for limit, vehicle and publishing requests', () => {
    for (const text of [
      'set altitude to 200 m', 'lower separation to 1 m', 'export the show', 'transmit now',
      'arm all drones', 'land now', 'send the show', 'upload it', 'start the show',
      'change the geofence', 'sign it', 'raise the speed limit',
      'make scene 1 a heart and export the show',
    ]) {
      const r = parseIntent(text, mkDoc());
      expect(r.ok, text).toBe(false);
      expect(r.reason, text).toMatch(/not supported/);
    }
  });

  it('refuses when there is no show or a non-string input', () => {
    expect(parseIntent('delete scene 1', { scenes: [] }).ok).toBe(false);
    expect(parseIntent('delete scene 1', null).ok).toBe(false);
    expect(parseIntent(null, mkDoc()).ok).toBe(false);
    expect(parseIntent(42, mkDoc()).ok).toBe(false);
  });

  it('refuses deleting the only scene', () => {
    const one = { scenes: [makeScene({ id: 'a' })], dronesList: [] };
    expect(parseIntent('delete scene 1', one).ok).toBe(false);
  });

  it('refuses a no-op edit with a reason', () => {
    const r = parseIntent('set hold of scene 1 to 6 s', mkDoc());
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/changes nothing/);
  });
});

describe('proposal pinned to a document', () => {
  it('a command built from document A is refused on a changed document B', () => {
    const a = mkDoc();
    const { parsed } = run('make scene 2 a heart', a);
    const changes = [
      applyCommand(a, { type: 'scene/delete', id: 'd' }),
      applyCommand(a, { type: 'scene/reorder', from: 0, to: 1 }),
      applyCommand(a, { type: 'scene/add', scene: makeScene({ id: 'z', name: 'Z' }) }),
    ];
    for (const b of changes) {
      expect(b).not.toBe(a);
      expect(applyCommand(b, parsed.command)).toBe(b);
    }
    // Same ids and order but different content is refused too (signature pin).
    const edited = applyCommand(a, { type: 'scene/patch', id: 'a', patch: { holdDurationSeconds: 9 } });
    expect(ids(edited)).toEqual(ids(a));
    expect(applyCommand(edited, parsed.command)).toBe(edited);
    expect(parsed.command.expectSignature).toBe(sceneSignature(a));
  });
});
