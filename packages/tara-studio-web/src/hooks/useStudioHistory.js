import { useReducer, useCallback, useMemo, useEffect } from 'react';
import {
  createHistory, execute, undo, redo, jumpTo, reset, canUndo, canRedo, timeline,
} from '../engine/history.js';
import { applyCommand } from '../engine/commands.js';
import { historyShortcut } from '../engine/shortcuts.js';

const reducer = (history, action) => {
  switch (action.type) {
    case 'execute': return execute(history, applyCommand, action.command, action.now);
    case 'undo': return undo(history);
    case 'redo': return redo(history);
    case 'jump': return jumpTo(history, action.applied);
    case 'reset': {
      const doc = typeof action.doc === 'function' ? action.doc(history.present) : action.doc;
      return reset(history, doc);
    }
    default: return history;
  }
};

/**
 * The Studio document and its undo/redo history (issue #100).
 *
 * `execute(command)` is the single entry point for editing the document:
 * commands come from engine/commands.js, and every one is undoable. `reset`
 * swaps in a different document and clears the history, for loading or
 * creating a project - opening another show is not an edit to this one.
 */
export default function useStudioHistory(initialDoc) {
  const [history, dispatch] = useReducer(reducer, initialDoc, createHistory);

  // The clock is read here, outside the reducer, so the reducer stays pure.
  const run = useCallback((command) => dispatch({ type: 'execute', command, now: Date.now() }), []);
  const undoLast = useCallback(() => dispatch({ type: 'undo' }), []);
  const redoNext = useCallback(() => dispatch({ type: 'redo' }), []);
  const jump = useCallback((applied) => dispatch({ type: 'jump', applied }), []);
  const resetDoc = useCallback((doc) => dispatch({ type: 'reset', doc }), []);

  useEffect(() => {
    const onKeyDown = (event) => {
      const action = historyShortcut(event);
      if (!action) return;
      event.preventDefault();
      dispatch({ type: action });
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  const entries = useMemo(() => timeline(history), [history]);

  return {
    doc: history.present,
    execute: run,
    undo: undoLast,
    redo: redoNext,
    jumpTo: jump,
    reset: resetDoc,
    canUndo: canUndo(history),
    canRedo: canRedo(history),
    // Entries in order; the first `applied` are done, the rest are undone.
    entries,
    applied: history.past.length,
  };
}
