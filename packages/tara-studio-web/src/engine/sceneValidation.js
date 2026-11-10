// Strict validation of scene commands (issue #89). The plain reducer in
// commands.js tolerates anything and turns bad input into silent no-ops; this
// is the fail-closed gate used for batches and for parsed intents: a command
// either passes with { ok: true } or is refused with a reason.

import { SHAPE_LIBRARY } from './formations.js';
import { SCENE_LIMITS, MAX_SCENE_NAME_LENGTH } from './sceneLimits.js';

/** The only scene fields an edit may set. Notably not `id`. */
export const PATCHABLE_FIELDS = Object.freeze([
  'shapeType', 'shapePayload', 'scale', 'holdDurationSeconds',
  'transitionDurationSeconds', 'transitLayerGapMeters', 'name',
]);

const ALLOWED_TYPES = new Set([
  'scene/add', 'scene/patch', 'scene/duplicate', 'scene/delete', 'scene/move', 'scene/reorder',
]);

const OK = Object.freeze({ ok: true });
const refuse = (reason) => ({ ok: false, reason });
const has = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);
const isPlainObject = (v) => v != null && typeof v === 'object' && !Array.isArray(v);

export const isLibraryShape = (shapeType, shapePayload) =>
  SHAPE_LIBRARY.some((e) => e.shapeType === shapeType && e.shapePayload === shapePayload);

/** Returns a reason string if `value` is not acceptable for `key`, else null. */
const fieldProblem = (key, value) => {
  if (key === 'name') {
    if (typeof value !== 'string' || !value.trim()) return 'name must be a non-empty string';
    if (value.length > MAX_SCENE_NAME_LENGTH) return `name is longer than ${MAX_SCENE_NAME_LENGTH} characters`;
    return null;
  }
  if (key === 'shapeType' || key === 'shapePayload') {
    return typeof value === 'string' && value ? null : `${key} must be a non-empty string`;
  }
  if (!has(SCENE_LIMITS, key)) return `${key} is not an editable scene field`;
  if (typeof value !== 'number' || !Number.isFinite(value)) return `${key} must be a finite number`;
  const { min, max } = SCENE_LIMITS[key];
  if (value < min || value > max) return `${key} ${value} is outside the allowed range ${min} to ${max}`;
  return null;
};

// JSON with object keys sorted at every level, so equal content always gives
// the same string whatever order the keys were written in.
const stable = (v) => {
  if (Array.isArray(v)) return `[${v.map(stable).join(',')}]`;
  if (v && typeof v === 'object') {
    return `{${Object.keys(v).sort()
      .filter((k) => v[k] !== undefined)
      .map((k) => `${JSON.stringify(k)}:${stable(v[k])}`).join(',')}}`;
  }
  return JSON.stringify(v) ?? 'null';
};

/**
 * A deterministic string of the full scene content (every field of every scene,
 * in order). A proposal records it so it can be refused if anything about the
 * scenes - not just their ids - changed afterwards.
 */
export const sceneSignature = (doc) => stable(Array.isArray(doc?.scenes) ? doc.scenes : []);

const validIndex = (i, n) => Number.isInteger(i) && i >= 0 && i < n;

/**
 * @param {{scenes: object[]}} doc the document the command would apply to
 * @returns {{ok: true} | {ok: false, reason: string}}
 */
export const validateSceneCommand = (doc, command) => {
  if (!doc || !Array.isArray(doc.scenes)) return refuse('document has no scene list');
  if (!isPlainObject(command) || typeof command.type !== 'string') return refuse('not a command');
  if (!ALLOWED_TYPES.has(command.type)) return refuse(`"${command.type}" is not an allowed scene command`);
  const { scenes } = doc;

  switch (command.type) {
    case 'scene/add': {
      const s = command.scene;
      if (!isPlainObject(s)) return refuse('scene/add has no scene');
      const allowed = new Set(['id', ...PATCHABLE_FIELDS]);
      const stray = Object.keys(s).find((k) => !allowed.has(k));
      if (stray) return refuse(`scene/add carries unknown field "${stray}"`);
      if (typeof s.id !== 'string' || !s.id) return refuse('scene/add needs a scene id');
      if (scenes.some((x) => x.id === s.id)) return refuse(`scene id "${s.id}" already exists`);
      if (s.name != null) {
        const p = fieldProblem('name', s.name);
        if (p) return refuse(p);
      }
      for (const key of ['shapeType', 'shapePayload', 'scale', 'holdDurationSeconds',
        'transitionDurationSeconds', 'transitLayerGapMeters']) {
        if (!has(s, key)) return refuse(`scene/add is missing ${key}`);
        const p = fieldProblem(key, s[key]);
        if (p) return refuse(p);
      }
      if (!isLibraryShape(s.shapeType, s.shapePayload)) {
        return refuse(`shape ${s.shapeType}/${s.shapePayload} is not in the shape library`);
      }
      return OK;
    }

    case 'scene/patch': {
      const scene = scenes.find((s) => s.id === command.id);
      if (!scene) return refuse(`scene "${command.id}" does not exist`);
      const patch = command.patch;
      if (!isPlainObject(patch)) return refuse('patch must be an object');
      const keys = Object.keys(patch);
      if (!keys.length) return refuse('patch is empty');
      for (const key of keys) {
        if (!PATCHABLE_FIELDS.includes(key)) return refuse(`field "${key}" may not be edited`);
        const p = fieldProblem(key, patch[key]);
        if (p) return refuse(p);
      }
      if (has(patch, 'shapeType') || has(patch, 'shapePayload')) {
        const t = patch.shapeType ?? scene.shapeType;
        const pl = patch.shapePayload ?? scene.shapePayload;
        if (!isLibraryShape(t, pl)) return refuse(`shape ${t}/${pl} is not in the shape library`);
      }
      if (keys.every((k) => Object.is(scene[k], patch[k]))) return refuse('patch changes nothing');
      return OK;
    }

    case 'scene/duplicate': {
      if (!scenes.some((s) => s.id === command.id)) return refuse(`scene "${command.id}" does not exist`);
      if (typeof command.copyId !== 'string' || !command.copyId) return refuse('duplicate needs a copy id');
      if (scenes.some((s) => s.id === command.copyId)) return refuse(`scene id "${command.copyId}" already exists`);
      return OK;
    }

    case 'scene/delete': {
      if (!scenes.some((s) => s.id === command.id)) return refuse(`scene "${command.id}" does not exist`);
      if (scenes.length <= 1) return refuse('a show needs at least one scene');
      return OK;
    }

    case 'scene/move': {
      if (!validIndex(command.index, scenes.length)) return refuse(`scene index ${command.index} is out of range`);
      if (command.dir !== -1 && command.dir !== 1) return refuse('move direction must be -1 or 1');
      if (!validIndex(command.index + command.dir, scenes.length)) return refuse('cannot move the scene past the end of the list');
      return OK;
    }

    case 'scene/reorder': {
      if (!validIndex(command.from, scenes.length)) return refuse(`scene index ${command.from} is out of range`);
      if (!validIndex(command.to, scenes.length)) return refuse(`scene index ${command.to} is out of range`);
      if (command.from === command.to) return refuse('scene is already in that position');
      return OK;
    }

    default:
      return OK;
  }
};
