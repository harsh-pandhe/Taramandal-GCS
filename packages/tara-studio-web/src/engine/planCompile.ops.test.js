import { describe, expect, it } from 'vitest';
import { makeScene } from './sequencer.js';
import { batchRefusal, applyCommand } from './commands.js';
import { compilePlan } from './planCompile.js';
import { describeChanges } from './proposalView.js';

// Planner ops move / delete / duplicate (issue #94, slice 2). Scene numbers always mean the numbering
// of the document the draft was requested for; each op is resolved to an id at once.
const four = () => ({
  scenes: ['a', 'b', 'c', 'd'].map((id, i) => makeScene({ id, name: `S${i + 1}` })),
  dronesList: [],
});
const ok = (ops, unsupported = []) => ({ status: 'ok', ops, unsupported });
const ids = (d) => d.scenes.map((s) => s.id);
const run = (doc, ...ops) => {
  const r = compilePlan(doc, ok(ops));
  return { r, after: r.ok ? applyCommand(doc, r.command) : null };
};

describe('each new op compiles by scene id into ONE pinned batch', () => {
  it('delete', () => {
    const d = four();
    const { r, after } = run(d, { op: 'delete', scene: 2 });
    expect(r.ok).toBe(true);
    expect(r.command.type).toBe('scene/batch');
    expect(r.command.commands).toEqual([expect.objectContaining({ type: 'scene/delete', id: 'b' })]);
    expect(r.command.expectIds).toEqual(['a', 'b', 'c', 'd']);
    expect(r.command.mergeKey).toBeUndefined();
    expect(ids(after)).toEqual(['a', 'c', 'd']);
    expect(r.summary).toBe('Planner: Delete scene 2 ("S2")');
  });

  it('duplicate puts the copy right after the original', () => {
    const d = four();
    const { r, after } = run(d, { op: 'duplicate', scene: 2 });
    expect(r.command.commands[0]).toMatchObject({ type: 'scene/duplicate', id: 'b' });
    expect(after.scenes.map((s) => s.name)).toEqual(['S1', 'S2', 'S2 copy', 'S3', 'S4']);
    expect(r.summary).toBe('Planner: Duplicate scene 2 ("S2")');
  });

  it.each([
    [{ scene: 1, position: 'after', target: 3 }, ['b', 'c', 'a', 'd'], 'Move scene 1 ("S1") after scene 3'],
    [{ scene: 3, position: 'before', target: 1 }, ['c', 'a', 'b', 'd'], 'Move scene 3 ("S3") before scene 1'],
    [{ scene: 4, position: 'first' }, ['d', 'a', 'b', 'c'], 'Move scene 4 ("S4") to the start'],
    [{ scene: 2, position: 'last' }, ['a', 'c', 'd', 'b'], 'Move scene 2 ("S2") to the end'],
  ])('move %j', (op, order, summary) => {
    const d = four();
    const { r, after } = run(d, { op: 'move', ...op });
    expect(r.ok).toBe(true);
    expect(r.command.commands).toHaveLength(1);
    expect(r.command.commands[0].type).toBe('scene/reorder');
    expect(ids(after)).toEqual(order);
    expect(r.summary).toBe(`Planner: ${summary}`);
  });

  it('delete scene 2 and duplicate scene 1 is ONE batch (one undo step)', () => {
    const d = four();
    const { r, after } = run(d, { op: 'delete', scene: 2 }, { op: 'duplicate', scene: 1 });
    expect(r.ok).toBe(true);
    expect(r.command.commands.map((c) => c.type)).toEqual(['scene/delete', 'scene/duplicate']);
    expect(after.scenes.map((s) => s.name)).toEqual(['S1', 'S1 copy', 'S3', 'S4']);
    expect(r.summary).toBe('Planner: Delete scene 2 ("S2"); Duplicate scene 1 ("S1")');
    expect(describeChanges(d, r.command)).toEqual([
      { label: 'Scene 2 "S2"', value: 'deleted' },
      expect.objectContaining({ label: 'New scene 2 "S1 copy"' }),
    ]);
  });
});

describe('the card shows each new op correctly (describeChanges)', () => {
  it('delete -> "deleted", duplicate -> a new scene "X copy", move -> position rows', () => {
    const d = four();
    expect(describeChanges(d, run(d, { op: 'delete', scene: 3 }).r.command)).toEqual([
      { label: 'Scene 3 "S3"', value: 'deleted' },
    ]);
    expect(describeChanges(d, run(d, { op: 'duplicate', scene: 1 }).r.command)).toEqual([
      expect.objectContaining({ label: 'New scene 2 "S1 copy"' }),
    ]);
    expect(describeChanges(d, run(d, { op: 'move', scene: 4, position: 'first' }).r.command)).toEqual([
      { label: 'Scene "S4" position', value: '4 → 1' },
      { label: 'Scene "S1" position', value: '1 → 2' },
      { label: 'Scene "S2" position', value: '2 → 3' },
      { label: 'Scene "S3" position', value: '3 → 4' },
    ]);
  });
});

describe('numbering cannot shift mid-draft', () => {
  it('refuses to touch a scene an earlier op of the same draft deleted', () => {
    const d = four();
    for (const second of [
      { op: 'patch', scene: 2, scale: 2 },
      { op: 'duplicate', scene: 2 },
      { op: 'delete', scene: 2 },
      { op: 'move', scene: 2, position: 'last' },
      { op: 'move', scene: 1, position: 'after', target: 2 },
    ]) {
      const r = compilePlan(d, ok([{ op: 'delete', scene: 2 }, second]));
      expect(r.ok).toBe(false);
      expect(r.kind).toBe('refused');
      expect(r.reason).toMatch(/op 2: scene 2 \("S2"\) was deleted earlier in this draft/);
    }
  });

  it('numbers keep the original meaning after a delete: delete 1 then patch 3 edits the original scene 3', () => {
    const d = four();
    const { after } = run(d, { op: 'delete', scene: 1 }, { op: 'patch', scene: 3, scale: 2 });
    expect(after.scenes.map((s) => [s.id, s.scale])).toEqual([['b', 1], ['c', 2], ['d', 1]]);
  });

  it('a move after a delete or a duplicate lands where the operator meant', () => {
    const d = four();
    // original numbering: scene 4 goes before scene 2, although scene 1 is already gone.
    expect(ids(run(d, { op: 'delete', scene: 1 }, { op: 'move', scene: 4, position: 'before', target: 2 }).after))
      .toEqual(['d', 'b', 'c']);
    const dup = run(d, { op: 'duplicate', scene: 1 }, { op: 'move', scene: 4, position: 'first' });
    const copyId = dup.r.command.commands[0].copyId;
    expect(ids(dup.after)).toEqual(['d', 'a', copyId, 'b', 'c']);
    // moving scene 3 to the end after a duplicate of scene 1: the copy shifted every index by one.
    const late = run(d, { op: 'duplicate', scene: 1 }, { op: 'move', scene: 3, position: 'last' });
    expect(ids(late.after).filter((x) => x !== late.r.command.commands[0].copyId)).toEqual(['a', 'b', 'd', 'c']);
  });

  it('an added scene counts for the "last scene" rule and for the running order', () => {
    const one = { scenes: [makeScene({ id: 'a', name: 'Only' })], dronesList: [] };
    const r = compilePlan(one, ok([
      { op: 'add', scene: 0, shape_type: 'parametric', shape_payload: 'grid' },
      { op: 'delete', scene: 1 },
    ]));
    expect(r.ok).toBe(true);
    expect(applyCommand(one, r.command).scenes).toHaveLength(1);
  });
});

describe('refusals are clear and final (kind "refused")', () => {
  it('never deletes the last scene', () => {
    const one = { scenes: [makeScene({ id: 'a', name: 'Only' })], dronesList: [] };
    const r = compilePlan(one, ok([{ op: 'delete', scene: 1 }]));
    expect(r).toMatchObject({ ok: false, kind: 'refused' });
    expect(r.reason).toBe('op 1: cannot delete scene 1 ("Only"): a show needs at least one scene');
  });

  it('deleting every scene of a draft is refused at the last one', () => {
    const two = { scenes: [makeScene({ id: 'a', name: 'A' }), makeScene({ id: 'b', name: 'B' })], dronesList: [] };
    const r = compilePlan(two, ok([{ op: 'delete', scene: 1 }, { op: 'delete', scene: 2 }]));
    expect(r).toMatchObject({ ok: false, kind: 'refused' });
    expect(r.reason).toMatch(/op 2: cannot delete scene 2 .*at least one scene/);
  });

  it.each([
    [{ scene: 1, position: 'first' }],
    [{ scene: 4, position: 'last' }],
    [{ scene: 1, position: 'before', target: 2 }],
    [{ scene: 2, position: 'after', target: 1 }],
  ])('a move to the position the scene already has is refused: %j', (op) => {
    const r = compilePlan(four(), ok([{ op: 'move', ...op }]));
    expect(r).toMatchObject({ ok: false, kind: 'refused' });
    expect(r.reason).toMatch(/is already in that position/);
  });
});

describe('malformed new ops are schema problems (they can be repaired once)', () => {
  it.each([
    [{ op: 'move', scene: 1 }, /position must be one of/],
    [{ op: 'move', scene: 1, position: 'middle' }, /position must be one of/],
    [{ op: 'move', scene: 1, position: 'before' }, /needs a target scene number/],
    [{ op: 'move', scene: 1, position: 'after', target: 'x' }, /needs a target scene number/],
    [{ op: 'move', scene: 1, position: 'first', target: 2 }, /takes no target/],
    [{ op: 'move', scene: 2, position: 'before', target: 2 }, /cannot be moved before itself/],
    [{ op: 'move', scene: 1, position: 'after', target: 9 }, /scene 9 does not exist/],
    [{ op: 'delete', scene: 9 }, /scene 9 does not exist \(the show has 4\)/],
    [{ op: 'duplicate', scene: 0 }, /scene 0 does not exist/],
    [{ op: 'delete', scene: 1.5 }, /does not exist/],
    [{ op: 'explode', scene: 1 }, /"explode" is not a supported op \(patch, add, move, delete or duplicate\)/],
  ])('%j', (op, reason) => {
    const r = compilePlan(four(), ok([op]));
    expect(r).toMatchObject({ ok: false, kind: 'schema' });
    expect(r.reason).toMatch(reason);
  });

  it('never lets a model-supplied id through on the new ops', () => {
    const d = four();
    const { r } = run(d, { op: 'delete', scene: 2, id: 'a' }, { op: 'duplicate', scene: 1, copyId: 'a', id: 'd' });
    expect(r.command.commands[0].id).toBe('b');
    expect(r.command.commands[1]).toMatchObject({ id: 'a' });
    expect(r.command.commands[1].copyId).not.toBe('a');
  });
});

describe('the backend\'s own "needs a scene number" answer is one clear message', () => {
  it('is shown as it is, with no "not supported" prefix and no extra hint', () => {
    const r = compilePlan(four(), ok([], ["deleting a scene needs a scene number, e.g. 'delete scene 2'"]));
    expect(r).toMatchObject({ ok: false, kind: 'unsupported' });
    expect(r.reason).toBe("Deleting a scene needs a scene number, e.g. 'delete scene 2'.");
  });
});

describe('stale drafts', () => {
  it.each([
    [{ op: 'delete', scene: 2 }],
    [{ op: 'duplicate', scene: 2 }],
    [{ op: 'move', scene: 2, position: 'last' }],
  ])('%j goes stale when the scenes change afterwards', (op) => {
    const d = four();
    const r = compilePlan(d, ok([op]));
    const renamed = { ...d, scenes: [{ ...d.scenes[0], name: 'Renamed' }, ...d.scenes.slice(1)] };
    expect(batchRefusal(renamed, r.command)).toMatch(/^stale/);
    expect(applyCommand(renamed, r.command)).toBe(renamed);
  });
});

// ---- Fuzz: random valid drafts compile to exactly what applying the ops one by one gives -----------------
// The oracle below does not use sceneOrder.js or planCompile.js: scenes are tokens in a list, a move
// removes the token and inserts it next to the other one, a duplicate inserts "copy:<token>" after it.
const rng = (seed) => () => {
  seed = (seed * 1664525 + 1013904223) >>> 0; // eslint-disable-line no-param-reassign
  return seed / 2 ** 32;
};

const oracle = (list, op, scaleOf) => {
  const next = [...list];
  const at = (t) => next.indexOf(t);
  if (op.kind === 'patch') { scaleOf[op.token] = op.scale; return next; }
  if (op.kind === 'delete') { next.splice(at(op.token), 1); return next; }
  if (op.kind === 'duplicate') { next.splice(at(op.token) + 1, 0, `copy:${op.token}`); return next; }
  next.splice(at(op.token), 1);
  if (op.position === 'first') next.unshift(op.token);
  else if (op.position === 'last') next.push(op.token);
  else next.splice(at(op.other) + (op.position === 'after' ? 1 : 0), 0, op.token);
  return next;
};

describe('fuzz: random valid drafts equal the ops applied one by one by hand', () => {
  it('never throws, and the compiled result matches the oracle (or refuses a no-op move)', () => {
    const seen = { compiled: 0, refused: 0, move: 0, delete: 0, duplicate: 0, patch: 0, mixed: 0 };
    for (let seed = 1; seed <= 400; seed += 1) {
      const rand = rng(seed);
      const pick = (arr) => arr[Math.floor(rand() * arr.length)];
      const n = 2 + Math.floor(rand() * 4);
      const doc = {
        scenes: Array.from({ length: n }, (_, i) => makeScene({ id: `id${i}`, name: `S${i + 1}` })),
        dronesList: [],
      };
      let list = doc.scenes.map((s) => s.id);
      const alive = new Set(list);
      const scaleOf = {};
      const numberOf = (t) => Number(t.slice(2)) + 1;
      const ops = [];
      const hand = [];
      let expectRefusal = false;
      for (let k = 0, count = 1 + Math.floor(rand() * 3); k < count && !expectRefusal; k += 1) {
        const tokens = [...alive];
        const kind = pick(['patch', 'delete', 'duplicate', 'move', 'move']);
        const token = pick(tokens);
        if (kind === 'patch') {
          const scale = pick([0.5, 1.5, 2, 2.5]);
          ops.push({ op: 'patch', scene: numberOf(token), scale });
          hand.push({ kind, token, scale });
        } else if (kind === 'delete') {
          if (list.length <= 1) continue;
          ops.push({ op: 'delete', scene: numberOf(token) });
          hand.push({ kind, token });
          alive.delete(token);
        } else if (kind === 'duplicate') {
          ops.push({ op: 'duplicate', scene: numberOf(token) });
          hand.push({ kind, token });
        } else {
          const position = pick(['before', 'after', 'first', 'last']);
          const others = tokens.filter((t) => t !== token);
          if ((position === 'before' || position === 'after') && others.length === 0) continue;
          const other = pick(others);
          ops.push({
            op: 'move', scene: numberOf(token), position, ...(other && (position === 'before' || position === 'after') ? { target: numberOf(other) } : {}),
          });
          hand.push({ kind, token, position, other });
        }
        const before = list;
        list = oracle(list, hand[hand.length - 1], scaleOf);
        // a move that leaves the order unchanged is a refusal (and ends the draft: nothing after it is meaningful)
        if (hand[hand.length - 1].kind === 'move' && before.join() === list.join()) expectRefusal = true;
      }
      if (ops.length === 0) continue;

      let r;
      expect(() => { r = compilePlan(doc, ok(ops)); }).not.toThrow();
      if (expectRefusal) {
        expect(r.ok, `seed ${seed} ${JSON.stringify(ops)}`).toBe(false);
        expect(r.reason).toMatch(/already in that position/);
        seen.refused += 1;
        continue;
      }
      expect(r.ok, `seed ${seed} ${JSON.stringify(ops)} ${r.reason}`).toBe(true);
      seen.compiled += 1;
      if (new Set(ops.map((o) => o.op)).size > 1) seen.mixed += 1;
      for (const o of ops) seen[o.op] += 1;
      const result = applyCommand(doc, r.command);
      const original = new Set(doc.scenes.map((s) => s.id));
      const tokens = result.scenes.map((s) => (original.has(s.id) ? s.id : `copy:${doc.scenes.find((o) => `${o.name} copy` === s.name).id}`));
      expect(tokens, `seed ${seed} ${JSON.stringify(ops)}`).toEqual(list);
      for (const s of result.scenes) {
        if (original.has(s.id) && scaleOf[s.id] !== undefined) expect(s.scale).toBe(scaleOf[s.id]);
      }
      // and it is still exactly one pinned batch
      expect(r.command.type).toBe('scene/batch');
      expect(batchRefusal(doc, r.command)).toBeNull();
    }
    // the fuzz must actually exercise every op, mixed drafts and the refusal path
    expect(seen.compiled).toBeGreaterThan(250);
    expect(seen.refused).toBeGreaterThan(5);
    expect(seen.mixed).toBeGreaterThan(80);
    for (const k of ['move', 'delete', 'duplicate', 'patch']) expect(seen[k], k).toBeGreaterThan(60);
  });
});
