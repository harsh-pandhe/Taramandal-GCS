// What the Studio's confirmation dialogs say and when they appear (issue #92).
// Kept out of the components so the rules are plain functions.

/**
 * What a key press inside a dialog should do, as a pure decision. `index` is
 * the focused element's position among the dialog's `count` focusable
 * elements (-1 when focus is outside them). Returns { type: 'cancel' },
 * { type: 'focus', index } (Tab wraps inside the dialog instead of leaving
 * it) or null for a key the dialog ignores.
 */
export const dialogKeyAction = ({ key, shiftKey = false }, index, count) => {
  if (key === 'Escape') return { type: 'cancel' };
  if (key !== 'Tab' || count === 0) return null;
  if (shiftKey) return { type: 'focus', index: index <= 0 ? count - 1 : index - 1 };
  return { type: 'focus', index: index < 0 || index >= count - 1 ? 0 : index + 1 };
};

/**
 * The confirmation, if any, a Project Vault action needs before it runs.
 * Deleting a project is permanent, so it always asks. Loading a project or
 * starting a new show replaces the editor, so it asks only while there are
 * unsaved changes to lose; a clean editor returns null and the action runs
 * at once, as before.
 */
export const confirmationFor = (action, { isDirty, projectName }) => {
  if (action.kind === 'delete') {
    return {
      title: 'Delete project',
      message: `Delete "${projectName}" and all of its saved versions from the Project Vault? This cannot be undone.`,
      confirmLabel: 'Delete project',
      danger: true,
    };
  }
  if (!isDirty) return null;
  const creating = action.kind === 'create';
  return {
    title: creating ? 'Start a new show' : 'Load project',
    message: creating
      ? 'The show on screen has unsaved changes. Starting a new show will lose them.'
      : `The show on screen has unsaved changes. Loading "${projectName}" will lose them.`,
    confirmLabel: creating ? 'Discard changes and start new' : 'Discard changes and load',
    danger: true,
  };
};

/** What the Transmit confirmation tells the operator. */
export const transmitMessage = (droneCount) => {
  const fleet = Number.isInteger(droneCount) ? `${droneCount} drone${droneCount === 1 ? '' : 's'}` : 'the fleet';
  return `This pushes the show for ${fleet} to the GCS fleet. Launch checks still run on the GCS dashboard.`;
};
