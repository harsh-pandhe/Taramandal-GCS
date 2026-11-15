import { describe, expect, it } from 'vitest';
import { moveTargetIndex } from './sceneOrder.js';
import { sceneCommands, applyCommand } from './commands.js';
import { makeScene } from './sequencer.js';

describe('moveTargetIndex', () => {
  // [from, anchor, relation, count, expected]: 4 scenes, 0-based.
  const TABLE = [
    [0, 2, 'after', 4, 2],   // a b c d -> b c a d
    [2, 0, 'after', 4, 1],   // a b c d -> a c b d
    [0, 2, 'before', 4, 1],  // a b c d -> b a c d
    [3, 1, 'before', 4, 1],  // a b c d -> a d b c
    [3, 0, 'after', 4, 1],
    [0, 3, 'after', 4, 3],
    [3, 0, 'before', 4, 0],
    [2, 0, 'first', 4, 0],
    [0, 0, 'first', 4, 0],
    [1, 0, 'last', 4, 3],
    [3, 0, 'last', 4, 3],
  ];
  it.each(TABLE)('from %i, anchor %i, %s of %i scenes -> %i', (from, anchor, relation, count, expected) => {
    expect(moveTargetIndex(from, anchor, relation, count)).toBe(expected);
  });

  it('is null for an unknown relation', () => {
    expect(moveTargetIndex(0, 1, 'sideways', 3)).toBeNull();
  });

  it('puts the scene where the relation says, for every pair, through the real reorder command', () => {
    const ids = ['a', 'b', 'c', 'd', 'e'];
    const doc = { scenes: ids.map((id) => makeScene({ id })) };
    for (let from = 0; from < ids.length; from += 1) {
      for (let anchor = 0; anchor < ids.length; anchor += 1) {
        if (anchor === from) continue;
        for (const relation of ['before', 'after']) {
          const to = moveTargetIndex(from, anchor, relation, ids.length);
          const after = applyCommand(doc, sceneCommands.reorder(from, to)).scenes.map((s) => s.id);
          const moved = after.indexOf(ids[from]);
          const anchorAt = after.indexOf(ids[anchor]);
          expect(moved).toBe(relation === 'before' ? anchorAt - 1 : anchorAt + 1);
          // Nothing else changed relative order.
          expect(after.filter((id) => id !== ids[from])).toEqual(ids.filter((id) => id !== ids[from]));
        }
      }
    }
  });
});
