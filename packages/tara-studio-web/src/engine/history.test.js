import { describe, it, expect } from 'vitest';
import {
  createHistory, execute, undo, redo, jumpTo, reset, canUndo, canRedo, timeline,
  DEFAULT_HISTORY_LIMIT, MERGE_WINDOW_MS,
} from './history.js';

// A document of one number and commands that add to it: enough to exercise the
// stack without involving scenes.
const add = (doc, command) => (command.by === 0 ? doc : { n: doc.n + command.by });
const by = (n, extra = {}) => ({ type: 'add', label: `add ${n}`, by: n, ...extra });
const run = (history, ...commands) =>
  commands.reduce((h, c, i) => execute(h, add, c, i * 10_000), history);

describe('history stack', () => {
  it('starts with nothing to undo or redo', () => {
    const h = createHistory({ n: 0 });
    expect(canUndo(h)).toBe(false);
    expect(canRedo(h)).toBe(false);
    expect(undo(h)).toBe(h);
    expect(redo(h)).toBe(h);
    expect(h.limit).toBe(DEFAULT_HISTORY_LIMIT);
  });

  it('undoes and redoes a sequence in order', () => {
    let h = run(createHistory({ n: 0 }), by(1), by(2), by(3));
    expect(h.present.n).toBe(6);

    h = undo(h);
    expect(h.present.n).toBe(3);
    h = undo(h);
    expect(h.present.n).toBe(1);
    h = redo(h);
    expect(h.present.n).toBe(3);
    h = redo(h);
    expect(h.present.n).toBe(6);
    expect(canRedo(h)).toBe(false);
  });

  it('undoes all the way back to the opening document', () => {
    const opened = { n: 5 };
    let h = run(createHistory(opened), by(1), by(1));
    h = undo(undo(h));
    expect(h.present).toBe(opened);
    expect(canUndo(h)).toBe(false);
  });

  it('restores the exact document object on redo instead of recomputing it', () => {
    const h1 = run(createHistory({ n: 0 }), by(1));
    const after = h1.present;
    expect(redo(undo(h1)).present).toBe(after);
  });

  it('drops the redo branch when a new command follows an undo', () => {
    let h = run(createHistory({ n: 0 }), by(1), by(2));
    h = undo(h);
    h = execute(h, add, by(10), 99_000);
    expect(h.present.n).toBe(11);
    expect(canRedo(h)).toBe(false);
    expect(timeline(h).map((e) => e.label)).toEqual(['add 1', 'add 10']);
    // The abandoned branch is unreachable: undo walks the new one.
    expect(undo(h).present.n).toBe(1);
  });

  it('does not record a command that changes nothing, and keeps the redo branch', () => {
    let h = run(createHistory({ n: 0 }), by(1), by(2));
    h = undo(h);
    const same = execute(h, add, by(0), 99_000);
    expect(same).toBe(h);
    expect(canRedo(same)).toBe(true);
  });

  it('never mutates the history it is given', () => {
    const h0 = createHistory({ n: 0 });
    const h1 = run(h0, by(1));
    const frozen = JSON.stringify(h1);
    undo(h1); redo(undo(h1)); execute(h1, add, by(5), 50_000); jumpTo(h1, 0);
    expect(JSON.stringify(h1)).toBe(frozen);
    expect(h0.past).toHaveLength(0);
  });
});

describe('bounded history', () => {
  it('drops the oldest entries past the limit', () => {
    let h = createHistory({ n: 0 }, 3);
    h = run(h, by(1), by(1), by(1), by(1), by(1));
    expect(h.past).toHaveLength(3);
    expect(h.present.n).toBe(5);
    // Only the last three edits can be undone; the first two are permanent.
    h = undo(undo(undo(h)));
    expect(h.present.n).toBe(2);
    expect(canUndo(h)).toBe(false);
  });

  it('holds at least one entry even for a nonsense limit', () => {
    const h = run(createHistory({ n: 0 }, 0), by(1), by(1));
    expect(h.past).toHaveLength(1);
  });
});

describe('merging bursts', () => {
  const typed = (n) => by(n, { mergeKey: 'name' });

  it('collapses same-key commands inside the window into one entry', () => {
    let h = createHistory({ n: 0 });
    h = execute(h, add, typed(1), 1000);
    h = execute(h, add, typed(1), 1000 + MERGE_WINDOW_MS);
    h = execute(h, add, typed(1), 1000 + 2 * MERGE_WINDOW_MS);
    expect(h.past).toHaveLength(1);
    expect(h.present.n).toBe(3);
    // One undo reverts the whole burst.
    expect(undo(h).present.n).toBe(0);
    expect(redo(undo(h)).present.n).toBe(3);
  });

  it('starts a new entry after a pause', () => {
    let h = createHistory({ n: 0 });
    h = execute(h, add, typed(1), 1000);
    h = execute(h, add, typed(1), 1000 + MERGE_WINDOW_MS + 1);
    expect(h.past).toHaveLength(2);
  });

  it('does not merge different keys or commands without a key', () => {
    let h = createHistory({ n: 0 });
    h = execute(h, add, by(1, { mergeKey: 'a' }), 1000);
    h = execute(h, add, by(1, { mergeKey: 'b' }), 1001);
    h = execute(h, add, by(1), 1002);
    h = execute(h, add, by(1), 1003);
    expect(h.past).toHaveLength(4);
  });

  it('does not merge into an entry across an undo/redo', () => {
    let h = createHistory({ n: 0 });
    h = execute(h, add, typed(1), 1000);
    h = execute(h, add, by(5), 1001);
    h = undo(h);
    // Top of the stack is the typed entry again, within the window, but there
    // is a redo branch: this is a new edit, not a continuation of the burst.
    h = execute(h, add, typed(1), 1002);
    expect(h.past).toHaveLength(2);
    expect(undo(h).present.n).toBe(1);
  });
});

describe('jumpTo and reset', () => {
  it('jumps to any point in the timeline, in either direction', () => {
    let h = run(createHistory({ n: 0 }), by(1), by(2), by(3));
    h = jumpTo(h, 1);
    expect(h.present.n).toBe(1);
    expect(h.future).toHaveLength(2);
    h = jumpTo(h, 3);
    expect(h.present.n).toBe(6);
    h = jumpTo(h, 0);
    expect(h.present.n).toBe(0);
  });

  it('clamps out-of-range targets', () => {
    const h = run(createHistory({ n: 0 }), by(1), by(2));
    expect(jumpTo(h, -4).present.n).toBe(0);
    expect(jumpTo(h, 99).present.n).toBe(3);
  });

  it('reset swaps the document, clears both stacks and keeps the limit', () => {
    let h = run(createHistory({ n: 0 }, 7), by(1), by(2));
    h = reset(undo(h), { n: 42 });
    expect(h.present.n).toBe(42);
    expect(canUndo(h)).toBe(false);
    expect(canRedo(h)).toBe(false);
    expect(h.limit).toBe(7);
  });
});
