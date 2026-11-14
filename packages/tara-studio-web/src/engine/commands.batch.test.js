import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { applyCommand, sceneCommands, batchRefusal, previewBatch } from './commands.js';
import { validateSceneCommand, sceneSignature } from './sceneValidation.js';
import { createHistory, execute, undo, redo, timeline } from './history.js';
import { makeScene } from './sequencer.js';
import { defaultFleet, fleetCommands } from './fleetCommands.js';

const mkDoc = () => ({
  scenes: [
    makeScene({ id: 'a', name: 'One' }),
    makeScene({ id: 'b', name: 'Two' }),
    makeScene({ id: 'c', name: 'Three' }),
  ],
  dronesList: defaultFleet(3),
});
const idsOf = (doc) => doc.scenes.map((s) => s.id);
const batch = (doc, commands) => sceneCommands.batch(commands, idsOf(doc), sceneSignature(doc));

describe('scene/batch', () => {
  it('applies every step in order', () => {
    const doc = mkDoc();
    const out = applyCommand(doc, batch(doc, [
      sceneCommands.patch('a', { holdDurationSeconds: 9 }),
      sceneCommands.patch('b', { shapeType: 'parametric', shapePayload: 'heart' }),
      sceneCommands.reorder(0, 2),
    ]));
    expect(out).not.toBe(doc);
    expect(idsOf(out)).toEqual(['b', 'c', 'a']);
    expect(out.scenes[2].holdDurationSeconds).toBe(9);
    expect(out.scenes[0].shapePayload).toBe('heart');
    expect(doc.scenes[0].holdDurationSeconds).toBe(6); // original untouched
  });

  it('is all-or-nothing: a valid step then an invalid one leaves the same document', () => {
    const doc = mkDoc();
    const cmd = batch(doc, [
      sceneCommands.patch('a', { holdDurationSeconds: 9 }),
      sceneCommands.patch('b', { holdDurationSeconds: 999 }),
    ]);
    expect(applyCommand(doc, cmd)).toBe(doc);
    expect(batchRefusal(doc, cmd)).toMatch(/step 2/);
  });

  it('validates against the document as it evolves', () => {
    const doc = mkDoc();
    const del = sceneCommands.delete('a');
    // Deleting a then patching a: the second step must be refused.
    expect(applyCommand(doc, batch(doc, [del, sceneCommands.patch('a', { scale: 2 })]))).toBe(doc);
    // Delete everything down to one and then one more is refused.
    const cmd = batch(doc, [sceneCommands.delete('a'), sceneCommands.delete('b'), sceneCommands.delete('c')]);
    expect(applyCommand(doc, cmd)).toBe(doc);
  });

  it('is one undo step: one entry, undo restores the exact doc, redo replays', () => {
    const doc = mkDoc();
    let h = createHistory(doc);
    h = execute(h, applyCommand, batch(doc, [
      sceneCommands.patch('a', { scale: 2 }),
      sceneCommands.patch('b', { scale: 1.5 }),
      sceneCommands.delete('c'),
    ]), 0);
    expect(timeline(h)).toHaveLength(1);
    const after = h.present;
    expect(after.scenes).toHaveLength(2);
    h = undo(h);
    expect(h.present).toBe(doc);
    h = redo(h);
    expect(h.present).toBe(after);
  });

  it('never merges with a manual edit made within 1 s, in either order', () => {
    const doc = mkDoc();
    const patch = sceneCommands.patch('a', { holdDurationSeconds: 7 });
    let h = execute(createHistory(doc), applyCommand, patch, 0);
    h = execute(h, applyCommand, batch(h.present, [sceneCommands.patch('a', { holdDurationSeconds: 8 })]), 100);
    expect(timeline(h)).toHaveLength(2);

    let g = execute(createHistory(doc), applyCommand, batch(doc, [sceneCommands.patch('a', { holdDurationSeconds: 8 })]), 0);
    g = execute(g, applyCommand, sceneCommands.patch('a', { holdDurationSeconds: 9 }), 100);
    expect(timeline(g)).toHaveLength(2);
    expect(sceneCommands.batch([], []).mergeKey).toBeUndefined();
  });

  it('refuses a batch that carries a mergeKey', () => {
    const doc = mkDoc();
    const cmd = { ...batch(doc, [sceneCommands.patch('a', { scale: 2 })]), mergeKey: 'x' };
    expect(applyCommand(doc, cmd)).toBe(doc);
  });

  it('refuses a stale batch (scene list changed since expectIds)', () => {
    const doc = mkDoc();
    const cmd = batch(doc, [sceneCommands.patch('a', { scale: 2 })]);
    const added = applyCommand(doc, sceneCommands.add({ name: 'New' }));
    expect(applyCommand(added, cmd)).toBe(added);
    expect(batchRefusal(added, cmd)).toMatch(/stale/);
    const reordered = applyCommand(doc, sceneCommands.reorder(0, 1));
    expect(applyCommand(reordered, cmd)).toBe(reordered);
    expect(applyCommand(doc, cmd)).not.toBe(doc); // still fine against its own doc
  });

  it('refuses when ids and order match but a scene changed after the proposal (content signature)', () => {
    const doc = mkDoc();
    const cmd = batch(doc, [sceneCommands.patch('b', { scale: 2 })]);
    const edited = applyCommand(doc, sceneCommands.patch('a', { holdDurationSeconds: 9 }));
    expect(idsOf(edited)).toEqual(idsOf(doc));
    expect(applyCommand(edited, cmd)).toBe(edited);
    expect(batchRefusal(edited, cmd)).toMatch(/scenes changed since the proposal was made/);
    expect(applyCommand(doc, cmd)).not.toBe(doc);
  });

  it('refuses a batch without expectSignature', () => {
    const doc = mkDoc();
    const { expectSignature, ...unpinned } = batch(doc, [sceneCommands.patch('a', { scale: 2 })]);
    expect(expectSignature).toEqual(expect.any(String));
    expect(applyCommand(doc, unpinned)).toBe(doc);
    expect(batchRefusal(doc, unpinned)).toMatch(/not pinned/);
    expect(applyCommand(doc, { ...unpinned, expectSignature: 42 })).toBe(doc);
  });

  it('sceneSignature is identical for identical content whatever the key order', () => {
    const a = { scenes: [{ id: 'x', name: 'N', scale: 1, nested: { p: 1, q: [{ b: 2, a: 1 }] } }] };
    const b = { scenes: [{ nested: { q: [{ a: 1, b: 2 }], p: 1 }, scale: 1, name: 'N', id: 'x' }] };
    expect(sceneSignature(a)).toBe(sceneSignature(b));
    expect(sceneSignature(a)).not.toBe(sceneSignature({ scenes: [{ ...a.scenes[0], scale: 2 }] }));
    expect(sceneSignature({ scenes: [a.scenes[0], { id: 'y' }] }))
      .not.toBe(sceneSignature({ scenes: [{ id: 'y' }, a.scenes[0]] }));
  });

  it('refuses an empty batch, a missing expectIds, and a nested batch', () => {
    const doc = mkDoc();
    expect(applyCommand(doc, batch(doc, []))).toBe(doc);
    expect(applyCommand(doc, { type: 'scene/batch', commands: [sceneCommands.patch('a', { scale: 2 })] })).toBe(doc);
    const inner = batch(doc, [sceneCommands.patch('a', { scale: 2 })]);
    const nested = batch(doc, [inner]);
    expect(applyCommand(doc, nested)).toBe(doc);
    expect(batchRefusal(doc, nested)).toMatch(/not an allowed scene command/);
    const mixed = batch(doc, [sceneCommands.patch('b', { scale: 2 }), inner]);
    expect(applyCommand(doc, mixed)).toBe(doc);
  });

  it('refuses patching id, unknown keys, bad numbers, out-of-range values and unknown shapes', () => {
    const doc = mkDoc();
    const bad = [
      { id: 'zzz' },
      { altitudeLimit: 500 },
      { minSeparation: 0.1 },
      { holdDurationSeconds: -1 },
      { holdDurationSeconds: 121 },
      { transitionDurationSeconds: 61 },
      { transitLayerGapMeters: 0.4 },
      { transitLayerGapMeters: 6.1 },
      { scale: 0.24 },
      { scale: 3.01 },
      { scale: NaN },
      { scale: Infinity },
      { scale: '2' },
      { name: '' },
      { name: 'x'.repeat(61) },
      { shapePayload: 'dragon' },
      { shapeType: 'mesh' },
      { shapeType: 'text', shapePayload: 'heart' },
      {},
    ];
    for (const p of bad) {
      const cmd = batch(doc, [sceneCommands.patch('a', p)]);
      expect(applyCommand(doc, cmd), JSON.stringify(p)).toBe(doc);
      expect(batchRefusal(doc, cmd), JSON.stringify(p)).toBeTruthy();
    }
  });

  it('accepts values on the range boundaries', () => {
    const doc = mkDoc();
    const out = applyCommand(doc, batch(doc, [sceneCommands.patch('a', {
      holdDurationSeconds: 120, transitionDurationSeconds: 0, transitLayerGapMeters: 0.5, scale: 3,
    })]));
    expect(out).not.toBe(doc);
  });

  it('refuses missing scene ids and out-of-range indices (silent no-ops in the plain reducer)', () => {
    const doc = mkDoc();
    const bad = [
      sceneCommands.patch('nope', { scale: 2 }),
      sceneCommands.delete('nope'),
      sceneCommands.duplicate('nope'),
      sceneCommands.move(-1, 1),
      sceneCommands.move(0, -1),
      sceneCommands.move(2, 1),
      sceneCommands.move(5, 1),
      sceneCommands.reorder(0, 3),
      sceneCommands.reorder(-1, 1),
      sceneCommands.reorder(1, 1),
      sceneCommands.reorder(0.5, 1),
    ];
    for (const c of bad) {
      expect(applyCommand(doc, batch(doc, [c])), c.type).toBe(doc);
      expect(validateSceneCommand(doc, c).ok, c.type).toBe(false);
    }
  });

  it('refuses scene/add with a bad shape, stray field or duplicate id', () => {
    const doc = mkDoc();
    const ok = sceneCommands.add({ name: 'N', shapeType: 'parametric', shapePayload: 'sphere' });
    expect(applyCommand(doc, batch(doc, [ok]))).not.toBe(doc);
    const dragon = sceneCommands.add({ shapePayload: 'dragon' });
    expect(applyCommand(doc, batch(doc, [dragon]))).toBe(doc);
    const stray = sceneCommands.add({ minSeparation: 0.1 });
    expect(applyCommand(doc, batch(doc, [stray]))).toBe(doc);
    const dup = sceneCommands.add({ id: 'a' });
    expect(applyCommand(doc, batch(doc, [dup]))).toBe(doc);
  });

  it('leaves existing commands behaving exactly as before', () => {
    const doc = mkDoc();
    // The plain reducer is still tolerant: this must stay a silent no-op.
    expect(applyCommand(doc, sceneCommands.patch('nope', { scale: 2 }))).toBe(doc);
    expect(applyCommand(doc, { type: 'bogus' })).toBe(doc);
    const spread = applyCommand(doc, sceneCommands.patch('a', { anything: 1 }));
    expect(spread.scenes[0].anything).toBe(1);
  });
});

describe('batch cannot smuggle anything else in', () => {
  const smuggled = [
    { type: 'export' },
    { type: 'transmit', target: 'gcs' },
    { type: 'fleet/add' },
    fleetCommands.setCount(5),
    fleetCommands.bulkUpdate([0], { hoverAlt: 100 }),
    { type: 'safety/set-limit', limit: 'altitude', value: 500 },
    { type: 'scene/patch', id: 'a', patch: { altitudeLimit: 500 } },
    { type: 'scene/patch', id: 'a', patch: { minSeparation: 0.1 } },
    { type: 'scene/set', id: 'a' },
    null,
    'scene/patch',
  ];

  for (const [i, bad] of smuggled.entries()) {
    it(`refuses smuggled command #${i + 1} after a valid step and leaves the doc untouched`, () => {
      const doc = mkDoc();
      const cmd = batch(doc, [sceneCommands.patch('a', { scale: 2 }), bad]);
      expect(applyCommand(doc, cmd)).toBe(doc);
      expect(batchRefusal(doc, cmd)).toMatch(/step 2/);
      expect(doc.scenes[0].scale).toBe(1);
      expect(doc.dronesList).toHaveLength(3);
    });
  }
});

describe('new engine files stay offline', () => {
  const files = ['sceneLimits.js', 'sceneValidation.js', 'intentParser.js'];
  for (const f of files) {
    it(`${f} has no network or api.js access`, () => {
      const src = readFileSync(fileURLToPath(new URL(`./${f}`, import.meta.url)), 'utf8');
      expect(src).not.toMatch(/from\s+['"][^'"]*api\.js['"]/);
      expect(src).not.toMatch(/import\([^)]*api\.js/);
      for (const banned of ['fetch(', 'XMLHttpRequest', 'WebSocket', '/api/']) {
        expect(src.includes(banned), `${f} contains ${banned}`).toBe(false);
      }
    });
  }
});

describe('previewBatch', () => {
  const edit = (doc) => batch(doc, [sceneCommands.patch('a', { holdDurationSeconds: 9 })]);

  it('returns the after-document without touching the original', () => {
    const doc = mkDoc();
    const r = previewBatch(doc, edit(doc));
    expect(r.ok).toBe(true);
    expect(r.doc.scenes[0].holdDurationSeconds).toBe(9);
    expect(doc.scenes[0].holdDurationSeconds).toBe(6);
    expect(r.doc).toEqual(applyCommand(doc, edit(doc)));
  });

  it('refuses a stale batch with the stale reason', () => {
    const doc = mkDoc();
    const cmd = edit(doc);
    const later = { ...doc, scenes: doc.scenes.map((s) => (s.id === 'b' ? { ...s, name: 'Renamed' } : s)) };
    const r = previewBatch(later, cmd);
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/^stale/);
  });

  it('refuses an invalid step with its step number, matching batchRefusal', () => {
    const doc = mkDoc();
    const cmd = batch(doc, [sceneCommands.patch('a', { holdDurationSeconds: 999 })]);
    const r = previewBatch(doc, cmd);
    expect(r).toEqual({ ok: false, reason: batchRefusal(doc, cmd) });
    expect(r.reason).toMatch(/step 1/);
  });

  it('is safe on garbage input', () => {
    expect(previewBatch(mkDoc(), null).ok).toBe(false);
  });
});
