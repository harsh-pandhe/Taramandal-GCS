import { describe, it, expect } from 'vitest';
import { makeScene } from './sequencer.js';
import { sceneCommands, applyCommand } from './commands.js';
import { createHistory, execute, undo, redo, MERGE_WINDOW_MS } from './history.js';

const doc = (names) => ({ scenes: names.map((n) => makeScene({ name: n })) });
const names = (d) => d.scenes.map((s) => s.name);
const ids = (d) => d.scenes.map((s) => s.id);

// Every scene-list operation the sequencer offers, as [label, build(doc)].
const OPERATIONS = [
  ['add', () => sceneCommands.add()],
  ['patch', (d) => sceneCommands.patch(d.scenes[1].id, { holdDurationSeconds: 99 })],
  ['duplicate', (d) => sceneCommands.duplicate(d.scenes[0].id)],
  ['delete', (d) => sceneCommands.delete(d.scenes[2].id)],
  ['move', () => sceneCommands.move(0, 1)],
  ['reorder', () => sceneCommands.reorder(0, 2)],
];

describe('scene commands', () => {
  it('add appends a scene named by its position, or by the given patch', () => {
    const d = applyCommand(doc(['A', 'B']), sceneCommands.add());
    expect(names(d)).toEqual(['A', 'B', 'Scene 3']);
    const named = applyCommand(d, sceneCommands.add({ name: 'Finale', shapeType: 'text', shapePayload: '9' }));
    expect(named.scenes[3]).toMatchObject({ name: 'Finale', shapeType: 'text', shapePayload: '9' });
  });

  it('patch changes only the addressed scene', () => {
    const d0 = doc(['A', 'B']);
    const d = applyCommand(d0, sceneCommands.patch(d0.scenes[1].id, { scale: 2 }));
    expect(d.scenes[1].scale).toBe(2);
    expect(d.scenes[0]).toBe(d0.scenes[0]);
  });

  it('duplicate inserts a renamed copy with a new id right after the original', () => {
    const d0 = doc(['A', 'B']);
    const d = applyCommand(d0, sceneCommands.duplicate(d0.scenes[0].id));
    expect(names(d)).toEqual(['A', 'A copy', 'B']);
    expect(new Set(ids(d)).size).toBe(3);
  });

  it('delete removes a scene but never the last one', () => {
    const d0 = doc(['A', 'B']);
    const d1 = applyCommand(d0, sceneCommands.delete(d0.scenes[0].id));
    expect(names(d1)).toEqual(['B']);
    expect(applyCommand(d1, sceneCommands.delete(d1.scenes[0].id))).toBe(d1);
  });

  it('move and reorder rearrange without changing membership', () => {
    const d0 = doc(['A', 'B', 'C', 'D']);
    expect(names(applyCommand(d0, sceneCommands.move(1, -1)))).toEqual(['B', 'A', 'C', 'D']);
    expect(names(applyCommand(d0, sceneCommands.reorder(0, 2)))).toEqual(['B', 'C', 'A', 'D']);
    expect(names(applyCommand(d0, sceneCommands.reorder(3, 1)))).toEqual(['A', 'D', 'B', 'C']);
  });

  it('returns the same document when a command has nothing to do', () => {
    const d = doc(['A', 'B']);
    const noOps = [
      sceneCommands.patch('no-such-id', { scale: 2 }),
      sceneCommands.patch(d.scenes[0].id, { name: 'A' }),
      sceneCommands.duplicate('no-such-id'),
      sceneCommands.delete('no-such-id'),
      sceneCommands.move(0, -1),
      sceneCommands.move(1, 1),
      sceneCommands.reorder(1, 1),
      sceneCommands.reorder(0, 9),
      sceneCommands.reorder(null, 1),
      { type: 'fleet/not-built-yet' },
      { type: 'scene/not-a-command' },
      null,
    ];
    for (const command of noOps) expect(applyCommand(d, command)).toBe(d);
  });

  it('never mutates the document it is given', () => {
    const d = doc(['A', 'B', 'C']);
    const before = JSON.stringify(d);
    for (const [, build] of OPERATIONS) applyCommand(d, build(d));
    expect(JSON.stringify(d)).toBe(before);
  });

  it('is deterministic: replaying a command gives the same scene ids', () => {
    const d = doc(['A']);
    for (const command of [sceneCommands.add(), sceneCommands.duplicate(d.scenes[0].id)]) {
      expect(ids(applyCommand(d, command))).toEqual(ids(applyCommand(d, command)));
    }
  });

  it('commands are plain serialisable data', () => {
    const d = doc(['A', 'B', 'C']);
    for (const [, build] of OPERATIONS) {
      const command = build(d);
      expect(JSON.parse(JSON.stringify(command))).toEqual(command);
      expect(typeof command.label).toBe('string');
    }
  });

  it('labels say what happened, for the history list', () => {
    expect(sceneCommands.add().label).toBe('Add scene');
    expect(sceneCommands.add({ name: 'Finale' }).label).toBe('Add scene "Finale"');
    expect(sceneCommands.patch('x', { name: 'N' }).label).toBe('Edit scene name');
    expect(sceneCommands.patch('x', { shapeType: 'text', shapePayload: '5' }).label).toBe('Edit scene shape');
    expect(sceneCommands.delete('x', 'Intro').label).toBe('Delete scene "Intro"');
    expect(sceneCommands.duplicate('x', 'Intro').label).toBe('Duplicate scene "Intro"');
    expect(sceneCommands.move(1, -1).label).toBe('Move scene up');
  });
});

describe('every scene-list operation is undoable and redoable', () => {
  it.each(OPERATIONS)('%s', (_label, build) => {
    const start = doc(['A', 'B', 'C']);
    const h0 = createHistory(start);
    const h1 = execute(h0, applyCommand, build(start), 0);

    expect(h1.present).not.toBe(start);
    expect(h1.past).toHaveLength(1);

    const undone = undo(h1);
    expect(undone.present).toBe(start);

    const redone = redo(undone);
    expect(redone.present).toBe(h1.present);
    expect(ids(redone.present)).toEqual(ids(h1.present));
  });

  it('a whole editing session unwinds to the opening scene list and back', () => {
    const start = doc(['A', 'B']);
    let h = createHistory(start);
    let t = 0;
    const step = (build) => { h = execute(h, applyCommand, build(h.present), (t += 10_000)); };

    step(() => sceneCommands.add({ name: 'C' }));
    step((d) => sceneCommands.duplicate(d.scenes[0].id));
    step((d) => sceneCommands.patch(d.scenes[1].id, { name: 'A2', scale: 1.5 }));
    step(() => sceneCommands.reorder(0, 3));
    step((d) => sceneCommands.delete(d.scenes[0].id));
    step(() => sceneCommands.move(0, 1));
    const end = h.present;
    expect(h.past).toHaveLength(6);

    for (let i = 0; i < 6; i++) h = undo(h);
    expect(h.present).toBe(start);
    for (let i = 0; i < 6; i++) h = redo(h);
    expect(h.present).toBe(end);
  });
});

describe('typing in a scene field', () => {
  it('merges keystrokes on one field into a single undo step', () => {
    const start = doc(['A']);
    const id = start.scenes[0].id;
    let h = createHistory(start);
    'Intro'.split('').forEach((_, i) => {
      h = execute(h, applyCommand, sceneCommands.patch(id, { name: 'Intro'.slice(0, i + 1) }), i * 100);
    });
    expect(h.present.scenes[0].name).toBe('Intro');
    expect(h.past).toHaveLength(1);
    expect(undo(h).present).toBe(start);
  });

  it('keeps edits to different fields or scenes as separate steps', () => {
    const start = doc(['A', 'B']);
    const [a, b] = start.scenes.map((s) => s.id);
    let h = createHistory(start);
    h = execute(h, applyCommand, sceneCommands.patch(a, { name: 'A1' }), 0);
    h = execute(h, applyCommand, sceneCommands.patch(a, { scale: 2 }), 10);
    h = execute(h, applyCommand, sceneCommands.patch(b, { scale: 2 }), 20);
    expect(h.past).toHaveLength(3);
  });

  it('does not merge keystrokes separated by a pause', () => {
    const start = doc(['A']);
    const id = start.scenes[0].id;
    let h = createHistory(start);
    h = execute(h, applyCommand, sceneCommands.patch(id, { name: 'In' }), 0);
    h = execute(h, applyCommand, sceneCommands.patch(id, { name: 'Intro' }), MERGE_WINDOW_MS + 1);
    expect(h.past).toHaveLength(2);
    expect(undo(h).present.scenes[0].name).toBe('In');
  });
});
