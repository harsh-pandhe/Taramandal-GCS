import { describe, expect, it } from 'vitest';
import { makeScene } from './sequencer.js';
import { batchRefusal, applyCommand } from './commands.js';
import { compilePlan, summariseScenes } from './planCompile.js';

const doc = () => ({
  scenes: [
    makeScene({ id: 'a', name: 'Opening' }),
    makeScene({ id: 'b', name: 'Finale', shapePayload: 'heart' }),
  ],
  dronesList: [],
});
const ok = (ops, unsupported = []) => ({ status: 'ok', ops, unsupported });

describe('summariseScenes', () => {
  it('lists 1-based numbers and shape tokens, never ids', () => {
    const d = doc();
    d.scenes.push(makeScene({ id: 'c', name: 'Seven', shapeType: 'text', shapePayload: '7' }));
    d.scenes.push(makeScene({ id: 'd', name: 'Flame', shapeType: 'svg', shapePayload: 'diya_flame' }));
    const s = summariseScenes(d);
    expect(s.map((x) => [x.number, x.shape])).toEqual([[1, 'circle'], [2, 'heart'], [3, 'text_7'], [4, 'svg_diya_flame']]);
    expect(s[0]).toEqual({ number: 1, name: 'Opening', shape: 'circle', scale: 1, hold_seconds: 6, transition_seconds: 4 });
    expect(JSON.stringify(s)).not.toContain('"a"');
  });

  it('is capped at the backend limit of 50 scenes', () => {
    const d = { scenes: Array.from({ length: 60 }, (_, i) => makeScene({ id: `s${i}` })) };
    expect(summariseScenes(d)).toHaveLength(50);
  });
});

describe('compilePlan', () => {
  it('compiles a patch into one pinned batch, resolving the number to the id', () => {
    const d = doc();
    const r = compilePlan(d, ok([{ op: 'patch', scene: 2, shape_type: 'parametric', shape_payload: 'sphere', hold_seconds: 8 }]));
    expect(r.ok).toBe(true);
    expect(r.command.type).toBe('scene/batch');
    expect(r.command.commands).toHaveLength(1);
    expect(r.command.commands[0]).toMatchObject({
      type: 'scene/patch', id: 'b',
      patch: { shapeType: 'parametric', shapePayload: 'sphere', holdDurationSeconds: 8 },
    });
    expect(r.command.expectIds).toEqual(['a', 'b']);
    expect(r.summary).toMatch(/^Planner: Edit scene 2 \("Finale"\): shape Sphere, hold 8 s$/);
    expect(batchRefusal(d, r.command)).toBeNull();
    expect(applyCommand(d, r.command).scenes[1].holdDurationSeconds).toBe(8);
  });

  it('compiles an add, keeping makeScene defaults for unset fields', () => {
    const d = doc();
    const r = compilePlan(d, ok([{ op: 'add', scene: 0, shape_type: 'parametric', shape_payload: 'grid', name: 'Grid' }]));
    expect(r.ok).toBe(true);
    const after = applyCommand(d, r.command);
    expect(after.scenes).toHaveLength(3);
    expect(after.scenes[2]).toMatchObject({ name: 'Grid', shapePayload: 'grid', holdDurationSeconds: 6, scale: 1 });
  });

  it('compiles several ops into ONE batch (one undo step)', () => {
    const r = compilePlan(doc(), ok([
      { op: 'patch', scene: 1, scale: 2 },
      { op: 'patch', scene: 2, transition_seconds: 9 },
      { op: 'add', scene: 0, shape_type: 'parametric', shape_payload: 'line' },
    ]));
    expect(r.ok).toBe(true);
    expect(r.command.commands.map((c) => c.type)).toEqual(['scene/patch', 'scene/patch', 'scene/add']);
  });

  it('refuses a scene number that does not exist', () => {
    const r = compilePlan(doc(), ok([{ op: 'patch', scene: 3, scale: 2 }]));
    expect(r).toMatchObject({ ok: false });
    expect(r.reason).toMatch(/scene 3 does not exist \(the show has 2\)/);
    expect(compilePlan(doc(), ok([{ op: 'patch', scene: 0, scale: 2 }])).ok).toBe(false);
    expect(compilePlan(doc(), ok([{ op: 'patch', scene: 1.5, scale: 2 }])).ok).toBe(false);
  });

  it('refuses a patch that changes nothing, with the engine reason', () => {
    const r = compilePlan(doc(), ok([{ op: 'patch', scene: 1, scale: 1 }]));
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/patch changes nothing/);
  });

  it('refuses an empty patch', () => {
    const r = compilePlan(doc(), ok([{ op: 'patch', scene: 1 }]));
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/changes no field/);
  });

  it.each([
    [{ scale: 9 }, /scale 9 is outside the allowed range 0\.25 to 3/],
    [{ hold_seconds: 500 }, /holdDurationSeconds 500 is outside the allowed range 0 to 120/],
    [{ transition_seconds: -1 }, /transitionDurationSeconds -1 is outside/],
  ])('refuses, never clamps, an out-of-range value %j', (fields, reason) => {
    const r = compilePlan(doc(), ok([{ op: 'patch', scene: 1, ...fields }]));
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/^step 1: /);
    expect(r.reason).toMatch(reason);
  });

  it('refuses a shape that is not in the library', () => {
    const r = compilePlan(doc(), ok([{ op: 'add', scene: 0, shape_type: 'parametric', shape_payload: 'dragon' }]));
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/not in the shape library/);
  });

  it('refuses malformed ops instead of guessing', () => {
    expect(compilePlan(doc(), ok([{ op: 'delete', scene: 1 }])).ok).toBe(false);
    expect(compilePlan(doc(), ok([{ op: 'add', scene: 0 }])).ok).toBe(false);
    expect(compilePlan(doc(), ok([{ op: 'patch', scene: 1, scale: '2' }])).ok).toBe(false);
    expect(compilePlan(doc(), ok([{ op: 'patch', scene: 1, scale: NaN }])).ok).toBe(false);
    expect(compilePlan(doc(), ok([null])).ok).toBe(false);
    const four = Array.from({ length: 4 }, () => ({ op: 'patch', scene: 1, scale: 2 }));
    expect(compilePlan(doc(), ok(four)).ok).toBe(false);
  });

  it('never lets a model-supplied id or extra field through', () => {
    const d = doc();
    const r = compilePlan(d, ok([{ op: 'patch', scene: 1, id: 'zzz', scale: 2, transitLayerGapMeters: 5, drones: 100 }]));
    expect(r.ok).toBe(true);
    expect(r.command.commands[0].id).toBe('a');
    expect(r.command.commands[0].patch).toEqual({ scale: 2 });
  });

  it('reports a request the model could only put in "unsupported" as a distinct kind', () => {
    const r = compilePlan(doc(), ok([], ['show length']));
    expect(r).toMatchObject({ ok: false, kind: 'unsupported' });
    expect(r.reason).toMatch(/show length/);
  });

  it('hands back what the model said it could not do, alongside a valid edit', () => {
    const r = compilePlan(doc(), ok([{ op: 'patch', scene: 1, scale: 2 }], ['turn the drones red']));
    expect(r.ok).toBe(true);
    expect(r.unsupported).toEqual(['turn the drones red']);
  });

  it('copes with repeated scene numbers, several ops on one scene and weird values without throwing', () => {
    const d = doc();
    const many = compilePlan(d, ok([
      { op: 'patch', scene: 1, scale: 2 }, { op: 'patch', scene: 1, hold_seconds: 9 }, { op: 'patch', scene: 1, name: 'New' },
    ]));
    expect(many.ok).toBe(true);
    expect(applyCommand(d, many.command).scenes[0]).toMatchObject({ scale: 2, holdDurationSeconds: 9, name: 'New' });
    for (const scene of [undefined, null, -1, 0, 0.5, NaN, Infinity, '1', [], {}, true]) {
      expect(() => compilePlan(d, ok([{ op: 'patch', scene, scale: 2 }]))).not.toThrow();
      expect(compilePlan(d, ok([{ op: 'patch', scene, scale: 2 }])).ok).toBe(false);
    }
    expect(compilePlan(d, ok([{ op: 'add', scene: 0, shape_type: 'parametric', shape_payload: 'grid', name: '' }])).ok).toBe(false);
    expect(compilePlan(d, { status: 'ok', ops: 'nope' }).ok).toBe(false);
    expect(compilePlan(d, ok([5, 'x', []])).ok).toBe(false);
  });

  it('refuses a non-ok result or a missing show', () => {
    expect(compilePlan(doc(), { status: 'unavailable' }).ok).toBe(false);
    expect(compilePlan({ scenes: [] }, ok([])).ok).toBe(false);
  });

  it('produces a command that goes stale when the scenes change afterwards', () => {
    const d = doc();
    const r = compilePlan(d, ok([{ op: 'patch', scene: 1, scale: 2 }]));
    const changed = { ...d, scenes: [{ ...d.scenes[0], name: 'Renamed' }, d.scenes[1]] };
    expect(batchRefusal(changed, r.command)).toMatch(/^stale/);
    expect(applyCommand(changed, r.command)).toBe(changed);
  });
});
