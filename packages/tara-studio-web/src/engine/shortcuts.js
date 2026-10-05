// Keyboard shortcuts for the history stack (issue #100).

const isTextEntry = (target) => {
  if (!target) return false;
  if (target.isContentEditable) return true;
  const tag = (target.tagName || '').toUpperCase();
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (tag !== 'INPUT') return false;
  // Sliders, checkboxes and colour pickers have no text undo of their own.
  const type = (target.type || 'text').toLowerCase();
  return !['range', 'checkbox', 'radio', 'color', 'button', 'submit', 'file'].includes(type);
};

/**
 * Maps a keydown event to 'undo', 'redo' or null.
 *
 *   Ctrl/Cmd+Z          undo
 *   Ctrl/Cmd+Shift+Z    redo
 *   Ctrl+Y              redo (the Windows convention)
 *
 * Returns null while a text field has focus: there Ctrl+Z belongs to the
 * browser's own text undo, and taking it would revert a scene edit when the
 * designer only meant to fix a typo.
 */
export const historyShortcut = (event) => {
  if (!event || event.altKey || !(event.ctrlKey || event.metaKey)) return null;
  if (isTextEntry(event.target)) return null;
  const key = (event.key || '').toLowerCase();
  if (key === 'z') return event.shiftKey ? 'redo' : 'undo';
  if (key === 'y' && event.ctrlKey && !event.metaKey && !event.shiftKey) return 'redo';
  return null;
};
