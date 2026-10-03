// Studio commands (issue #100): the only way the show document is edited.
//
// A command is plain data - { type, label, ...arguments } - built by the
// creators below and applied by `applyCommand`, a pure reducer. Nothing here
// mutates: a command returns a new document, or the same reference when it has
// nothing to do, which is how the history layer knows not to record it.
//
// Keeping commands as data rather than closures is deliberate. They can be
// logged, shown in a history list, and produced by tooling (the planned AI
// assistant, #89) without that tooling getting any other handle on Studio
// state - so everything it applies is undoable by construction.
//
// The document is { scenes, dronesList }. Other editable state (the LED track)
// joins it by adding a field and its command types here.

import { makeScene } from './sequencer.js';
import { MAX_FLEET_SIZE } from './api.js';
import { applyFleetCommand } from './fleetCommands.js';

const quote = (name) => `"${name}"`;

// What a patch changed, for the history list: "name", "hold", ...
const FIELD_LABELS = {
  name: 'name',
  shapeType: 'shape',
  shapePayload: 'shape',
  scale: 'scale',
  holdDurationSeconds: 'hold',
  transitionDurationSeconds: 'transition',
  transitLayerGapMeters: 'transit gap',
};

const patchLabel = (patch) => {
  const fields = [...new Set(Object.keys(patch).map((k) => FIELD_LABELS[k] ?? k))];
  return `Edit scene ${fields.join(', ')}`;
};

export const sceneCommands = {
  /**
   * The scene is built here, not in the reducer, so its id is fixed at the
   * moment the command is created and replaying it is deterministic.
   * A scene without a name gets "Scene N" when applied.
   */
  add: (patch = {}) => ({
    type: 'scene/add',
    label: patch.name ? `Add scene ${quote(patch.name)}` : 'Add scene',
    scene: makeScene({ name: null, ...patch }),
  }),

  /**
   * Consecutive patches to the same fields of the same scene merge into one
   * history entry (see history.js MERGE_WINDOW_MS).
   */
  patch: (id, patch) => ({
    type: 'scene/patch',
    label: patchLabel(patch),
    mergeKey: `scene/patch:${id}:${Object.keys(patch).sort().join(',')}`,
    id,
    patch,
  }),

  // `name` only feeds the history label; the reducer works from the id.
  duplicate: (id, name) => ({
    type: 'scene/duplicate',
    label: name ? `Duplicate scene ${quote(name)}` : 'Duplicate scene',
    id,
    // Fixed here for the same reason as `add`.
    copyId: makeScene().id,
  }),

  delete: (id, name) => ({
    type: 'scene/delete',
    label: name ? `Delete scene ${quote(name)}` : 'Delete scene',
    id,
  }),

  move: (index, dir) => ({
    type: 'scene/move',
    label: dir < 0 ? 'Move scene up' : 'Move scene down',
    index,
    dir,
  }),

  reorder: (from, to) => ({ type: 'scene/reorder', label: 'Reorder scenes', from, to }),
};

const applySceneCommand = (scenes, command) => {
  switch (command.type) {
    case 'scene/add': {
      const scene = command.scene.name == null
        ? { ...command.scene, name: `Scene ${scenes.length + 1}` }
        : command.scene;
      return [...scenes, scene];
    }

    case 'scene/patch': {
      const i = scenes.findIndex((s) => s.id === command.id);
      if (i < 0) return scenes;
      const changed = Object.keys(command.patch).some((k) => !Object.is(scenes[i][k], command.patch[k]));
      if (!changed) return scenes;
      return scenes.map((s) => (s.id === command.id ? { ...s, ...command.patch } : s));
    }

    case 'scene/duplicate': {
      const i = scenes.findIndex((s) => s.id === command.id);
      if (i < 0) return scenes;
      // Insert directly after the original - a duplicate appended to the end
      // would need dragging back into place, which is the common case.
      const copy = { ...scenes[i], id: command.copyId, name: `${scenes[i].name} copy` };
      return [...scenes.slice(0, i + 1), copy, ...scenes.slice(i + 1)];
    }

    case 'scene/delete': {
      // A show needs at least one scene; the UI disables the button too, but
      // enforce it here so no caller can empty the list.
      if (scenes.length <= 1 || !scenes.some((s) => s.id === command.id)) return scenes;
      return scenes.filter((s) => s.id !== command.id);
    }

    case 'scene/move': {
      const j = command.index + command.dir;
      if (command.index < 0 || command.index >= scenes.length || j < 0 || j >= scenes.length) return scenes;
      const copy = [...scenes];
      [copy[command.index], copy[j]] = [copy[j], copy[command.index]];
      return copy;
    }

    case 'scene/reorder': {
      const { from, to } = command;
      if (from === to || from == null || to == null) return scenes;
      if (from < 0 || from >= scenes.length || to < 0 || to >= scenes.length) return scenes;
      const copy = [...scenes];
      const [moved] = copy.splice(from, 1);
      copy.splice(to, 0, moved);
      return copy;
    }

    default:
      return scenes;
  }
};

/**
 * Applies one command to the Studio document. Unknown command types are a
 * no-op (the same document comes back) rather than an error, so a history
 * entry or a tool-supplied command from a newer build cannot break the editor.
 */
export const applyCommand = (doc, command) => {
  if (!command || typeof command.type !== 'string') return doc;
  if (command.type.startsWith('scene/')) {
    const scenes = applySceneCommand(doc.scenes, command);
    return scenes === doc.scenes ? doc : { ...doc, scenes };
  }
  if (command.type.startsWith('fleet/') && Array.isArray(doc.dronesList)) {
    const dronesList = applyFleetCommand(doc.dronesList, command, MAX_FLEET_SIZE);
    return dronesList === doc.dronesList ? doc : { ...doc, dronesList };
  }
  return doc;
};
