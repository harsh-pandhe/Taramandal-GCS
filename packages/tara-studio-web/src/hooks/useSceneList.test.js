import { describe, it, expect } from 'vitest';
import { makeScene } from '../engine/sequencer.js';

// The reorder/duplicate/delete rules are pure list transforms. Testing them
// directly (rather than through a rendered component) keeps this dependency-free
// — the package has no DOM testing stack — while still covering the behaviour
// issue #15 asks for: add, reorder, duplicate, delete.
//
// These mirror the reducers in useSceneList.js.

const reorder = (list, from, to) => {
  if (from === to || from == null || to == null) return list;
  if (from < 0 || from >= list.length || to < 0 || to >= list.length) return list;
  const copy = [...list];
  const [moved] = copy.splice(from, 1);
  copy.splice(to, 0, moved);
  return copy;
};

const move = (list, index, dir) => {
  const j = index + dir;
  if (j < 0 || j >= list.length) return list;
  const copy = [...list];
  [copy[index], copy[j]] = [copy[j], copy[index]];
  return copy;
};

const duplicate = (list, id) => {
  const i = list.findIndex((s) => s.id === id);
  if (i < 0) return list;
  const { id: _discarded, ...rest } = list[i];
  const copy = makeScene({ ...rest, name: `${list[i].name} copy` });
  return [...list.slice(0, i + 1), copy, ...list.slice(i + 1)];
};

const remove = (list, id) => (list.length <= 1 ? list : list.filter((s) => s.id !== id));

const mk = (names) => names.map((n) => makeScene({ name: n }));
const names = (list) => list.map((s) => s.name);

describe('scene reordering', () => {
  it('moves a scene to a later position', () => {
    expect(names(reorder(mk(['A', 'B', 'C', 'D']), 0, 2))).toEqual(['B', 'C', 'A', 'D']);
  });

  it('moves a scene to an earlier position', () => {
    expect(names(reorder(mk(['A', 'B', 'C', 'D']), 3, 1))).toEqual(['A', 'D', 'B', 'C']);
  });

  it('is a no-op for same index or out-of-range indices', () => {
    const list = mk(['A', 'B', 'C']);
    expect(names(reorder(list, 1, 1))).toEqual(['A', 'B', 'C']);
    expect(names(reorder(list, -1, 1))).toEqual(['A', 'B', 'C']);
    expect(names(reorder(list, 0, 9))).toEqual(['A', 'B', 'C']);
    expect(names(reorder(list, null, 1))).toEqual(['A', 'B', 'C']);
  });

  it('preserves length and membership', () => {
    const list = mk(['A', 'B', 'C', 'D']);
    const out = reorder(list, 2, 0);
    expect(out).toHaveLength(4);
    expect(new Set(names(out))).toEqual(new Set(['A', 'B', 'C', 'D']));
  });

  it('steps a scene up and down with the buttons', () => {
    expect(names(move(mk(['A', 'B', 'C']), 1, -1))).toEqual(['B', 'A', 'C']);
    expect(names(move(mk(['A', 'B', 'C']), 1, 1))).toEqual(['A', 'C', 'B']);
  });

  it('will not step past either end', () => {
    expect(names(move(mk(['A', 'B']), 0, -1))).toEqual(['A', 'B']);
    expect(names(move(mk(['A', 'B']), 1, 1))).toEqual(['A', 'B']);
  });
});

describe('duplicate', () => {
  it('inserts the copy directly after the original', () => {
    const list = mk(['A', 'B', 'C']);
    expect(names(duplicate(list, list[0].id))).toEqual(['A', 'A copy', 'B', 'C']);
    expect(names(duplicate(list, list[2].id))).toEqual(['A', 'B', 'C', 'C copy']);
  });

  it('gives the copy a fresh id but the same shape settings', () => {
    const list = [makeScene({ name: 'Five', shapeType: 'text', shapePayload: '5', scale: 1.7 })];
    const out = duplicate(list, list[0].id);
    expect(out[1].id).not.toBe(out[0].id);
    expect(out[1].shapeType).toBe('text');
    expect(out[1].shapePayload).toBe('5');
    expect(out[1].scale).toBe(1.7);
  });
});

describe('delete', () => {
  it('removes the named scene', () => {
    const list = mk(['A', 'B', 'C']);
    expect(names(remove(list, list[1].id))).toEqual(['A', 'C']);
  });

  it('refuses to empty the list', () => {
    const list = mk(['Only']);
    expect(names(remove(list, list[0].id))).toEqual(['Only']);
  });
});

describe('makeScene', () => {
  it('generates unique ids', () => {
    const ids = new Set(Array.from({ length: 500 }, () => makeScene({}).id));
    expect(ids.size).toBe(500);
  });

  it('applies a patch over the defaults', () => {
    const s = makeScene({ name: 'Five', holdDurationSeconds: 8 });
    expect(s.name).toBe('Five');
    expect(s.holdDurationSeconds).toBe(8);
    expect(s.shapeType).toBe('parametric'); // default preserved
    expect(s.transitLayerGapMeters).toBeGreaterThan(0);
  });
});
