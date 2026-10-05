// Undo/redo history over an immutable document (issue #100).
//
// A history is { past, present, future, limit }. `present` is the document
// being edited; `past` and `future` are entries, oldest first:
//
//   { command, label, at, before, after }
//
// Each entry keeps the document on both sides of its command, so undo and redo
// are a swap rather than a recomputation: redoing an "add scene" restores the
// same scene id it created the first time, and a command never needs a
// hand-written inverse. Documents share structure (an edit copies the list,
// not the scenes in it), so an entry costs one array, not a deep copy.
//
// Everything here is pure - no React, no clock unless one is passed in - so the
// same functions back the hook, the tests, and any tool that edits a show.

/** Entries kept before the oldest is dropped. */
export const DEFAULT_HISTORY_LIMIT = 100;

/**
 * Commands sharing a `mergeKey` within this window collapse into one entry.
 * Typing a scene name fires a command per keystroke; without merging, undo
 * would step back one letter at a time.
 */
export const MERGE_WINDOW_MS = 1000;

export const createHistory = (present, limit = DEFAULT_HISTORY_LIMIT) => ({
  past: [], present, future: [], limit: Math.max(1, limit),
});

export const canUndo = (history) => history.past.length > 0;
export const canRedo = (history) => history.future.length > 0;

/**
 * Applies `command` through `reducer(document, command)` and records it.
 *
 * A command that leaves the document unchanged (the reducer returns the same
 * reference) is not recorded and does not clear the redo branch. Any command
 * that does change it discards `future`: after an undo, a new edit starts a new
 * branch and the old one is gone.
 */
export const execute = (history, reducer, command, now = Date.now()) => {
  const after = reducer(history.present, command);
  if (after === history.present) return history;

  const top = history.past[history.past.length - 1];
  const merges = top
    && command.mergeKey != null
    && top.command.mergeKey === command.mergeKey
    && history.future.length === 0
    && now - top.at <= MERGE_WINDOW_MS;

  const entry = merges
    // Keep the first command's `before`, so one undo reverts the whole burst.
    ? { ...top, command, label: command.label ?? top.label, at: now, after }
    : { command, label: command.label ?? command.type, at: now, before: history.present, after };

  const past = merges ? [...history.past.slice(0, -1), entry] : [...history.past, entry];
  return {
    ...history,
    past: past.length > history.limit ? past.slice(past.length - history.limit) : past,
    present: after,
    future: [],
  };
};

export const undo = (history) => {
  if (!canUndo(history)) return history;
  const entry = history.past[history.past.length - 1];
  return {
    ...history,
    past: history.past.slice(0, -1),
    present: entry.before,
    future: [entry, ...history.future],
  };
};

export const redo = (history) => {
  if (!canRedo(history)) return history;
  const [entry, ...rest] = history.future;
  return {
    ...history,
    past: [...history.past, entry],
    present: entry.after,
    future: rest,
  };
};

/**
 * Moves to the point where exactly `applied` entries are done: 0 is the
 * document before any recorded edit, `past.length + future.length` is
 * everything redone. Out-of-range values clamp.
 */
export const jumpTo = (history, applied) => {
  const total = history.past.length + history.future.length;
  const target = Math.max(0, Math.min(total, Math.trunc(applied)));
  let h = history;
  while (h.past.length > target) h = undo(h);
  while (h.past.length < target) h = redo(h);
  return h;
};

/** Replaces the document and forgets all history, e.g. on loading a project. */
export const reset = (history, present) => createHistory(present, history.limit);

/** Every entry in order, undone ones included - what a history list shows. */
export const timeline = (history) => [...history.past, ...history.future];
