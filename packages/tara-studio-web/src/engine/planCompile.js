// Compiles a local-planner draft (issue #94) into ONE pinned scene/batch,
// or refuses with a reason. The planner's ops (patch, add, move, delete, duplicate) are scene NUMBERS and plain field
// values; ids, the batch pins and every limit come from here and from
// commands.js, never from the model. The result has the same shape as
// parseIntent's - { ok: true, command, summary } | { ok: false, reason } - so the
// proposal hook treats both sources alike, and the command is judged by the same
// previewBatch/batchRefusal before it can ever reach the proposal panel.
//
// Nothing here applies anything. Out-of-range values are not clamped: they go to
// previewBatch, whose reason is what comes back (and what is sent to the model as
// the one repair note).

import { SHAPE_LIBRARY } from './formations.js';
import { sceneCommands, previewBatch } from './commands.js';
import { sceneSignature } from './sceneValidation.js';
import { moveTargetIndex } from './sceneOrder.js';

/** The backend accepts at most this many scenes and ops (tara_studio/ai_plan.py). */
export const MAX_PLAN_SCENES = 50;
export const MAX_PLAN_OPS = 3;
const MOVE_POSITIONS = ['before', 'after', 'first', 'last'];

const fail = (reason, kind = 'refused') => ({ ok: false, reason, kind });

// The planner's shape vocabulary (ai_author.SHAPE_TOKENS): a parametric shape is
// its payload, others are "<type>_<payload>".
const shapeToken = (shapeType, shapePayload) => (
  shapeType === 'parametric' ? shapePayload : `${shapeType}_${shapePayload}`
);

const shapeLabel = (shapeType, shapePayload) => (
  SHAPE_LIBRARY.find((e) => e.shapeType === shapeType && e.shapePayload === shapePayload)?.label
  ?? `${shapeType}/${shapePayload}`
);

/** The compact scene list the model is shown: 1-based numbers, no ids. */
export const summariseScenes = (doc) => (doc?.scenes ?? []).slice(0, MAX_PLAN_SCENES).map((s, i) => ({
  number: i + 1,
  name: String(s.name ?? '').slice(0, 120),
  shape: shapeToken(s.shapeType, s.shapePayload),
  scale: s.scale,
  hold_seconds: s.holdDurationSeconds,
  transition_seconds: s.transitionDurationSeconds,
}));

const isNumber = (v) => typeof v === 'number' && Number.isFinite(v);

// Only these planner fields are read; anything else the model sent is ignored.
const numericFields = (op, at) => {
  const out = {};
  for (const [from, to] of [['scale', 'scale'], ['hold_seconds', 'holdDurationSeconds'], ['transition_seconds', 'transitionDurationSeconds']]) {
    if (op[from] === undefined) continue;
    if (!isNumber(op[from])) return { error: `op ${at}: ${from} must be a number` };
    out[to] = op[from];
  }
  return { fields: out };
};

const describeFields = (fields) => {
  const parts = [];
  if (fields.name !== undefined) parts.push(`name "${fields.name}"`);
  if (fields.shapeType !== undefined) parts.push(`shape ${shapeLabel(fields.shapeType, fields.shapePayload)}`);
  if (fields.scale !== undefined) parts.push(`scale ${fields.scale}`);
  if (fields.holdDurationSeconds !== undefined) parts.push(`hold ${fields.holdDurationSeconds} s`);
  if (fields.transitionDurationSeconds !== undefined) parts.push(`transition ${fields.transitionDurationSeconds} s`);
  return parts.join(', ');
};

/**
 * @param {{scenes: object[]}} doc  the document the plan was requested for
 * @param {{status: string, ops?: object[], unsupported?: string[]}} plan  backend result
 */
export const compilePlan = (doc, plan) => {
  if (!doc || !Array.isArray(doc.scenes) || doc.scenes.length === 0) return fail('there is no show to edit');
  if (!plan || plan.status !== 'ok') return fail('the planner returned no usable draft');
  const ops = Array.isArray(plan.ops) ? plan.ops : null;
  if (!ops) return fail('the planner draft has no ops list', 'schema');
  const unsupported = Array.isArray(plan.unsupported) ? plan.unsupported.filter((x) => typeof x === 'string') : [];
  if (ops.length === 0) {
    // An answer that already says what is wrong ("not supported", "needs a scene number": the backend's own guards) is shown as
    // it is, so the operator reads one sentence rather than "not supported" twice plus a hint.
    const selfExplaining = unsupported.length > 0 && unsupported.every((x) => /not supported|needs a scene number/i.test(x));
    let reason = 'the planner proposed no edit';
    if (selfExplaining) {
      const text = unsupported.join('; ');
      reason = `${text.charAt(0).toUpperCase()}${text.slice(1)}${/[.!?]$/.test(text) ? '' : '.'}`;
    } else if (unsupported.length) {
      reason = `not supported: ${unsupported.join('; ')}. The planner edits scene shapes, sizes, timing and names, and adds, moves, deletes or duplicates scenes.`;
    }
    return fail(reason, 'unsupported');
  }
  if (ops.length > MAX_PLAN_OPS) return fail(`at most ${MAX_PLAN_OPS} ops are allowed, got ${ops.length}`, 'schema');

  const ids = doc.scenes.map((s) => s.id);
  const commands = [];
  const parts = [];
  // Scene numbers in a draft always mean the numbering of the document it was requested for, so each
  // op is resolved to an id at once. `order` is where every scene sits at this point in the draft
  // (a move needs indices), and `gone` are the scenes an earlier op of the draft deleted.
  const order = [...ids];
  const gone = new Set();

  const refer = (n, at) => {
    if (!Number.isInteger(n) || n < 1 || n > doc.scenes.length) {
      return { error: fail(`op ${at}: scene ${n} does not exist (the show has ${doc.scenes.length})`, 'schema') };
    }
    const scene = doc.scenes[n - 1];
    if (gone.has(scene.id)) return { error: fail(`op ${at}: scene ${n} ("${scene.name}") was deleted earlier in this draft`) };
    return { scene };
  };

  for (const [k, op] of ops.entries()) {
    const at = k + 1;
    if (op === null || typeof op !== 'object') return fail(`op ${at} is not an object`, 'schema');
    const { fields, error } = numericFields(op, at);
    if (error) return fail(error, 'schema');
    if (op.name !== undefined) {
      if (typeof op.name !== 'string') return fail(`op ${at}: name must be a string`, 'schema');
      fields.name = op.name;
    }
    const hasShape = op.shape_type !== undefined || op.shape_payload !== undefined;
    if (hasShape) {
      if (typeof op.shape_type !== 'string' || typeof op.shape_payload !== 'string') {
        return fail(`op ${at}: shape must be a type and a payload`, 'schema');
      }
      fields.shapeType = op.shape_type;
      fields.shapePayload = op.shape_payload;
    }

    if (op.op === 'patch') {
      const ref = refer(op.scene, at);
      if (ref.error) return ref.error;
      if (Object.keys(fields).length === 0) return fail(`op ${at}: patch of scene ${op.scene} changes no field`, 'schema');
      commands.push(sceneCommands.patch(ref.scene.id, fields));
      parts.push(`Edit scene ${op.scene} ("${ref.scene.name}"): ${describeFields(fields)}`);
    } else if (op.op === 'add') {
      if (!hasShape) return fail(`op ${at}: add needs a shape`, 'schema');
      // Only the fields that were set: an undefined key would override makeScene's default.
      const add = sceneCommands.add(fields);
      commands.push(add);
      order.push(add.scene.id);
      parts.push(`Add a ${shapeLabel(fields.shapeType, fields.shapePayload)} scene${fields.name ? ` "${fields.name}"` : ''}`);
    } else if (op.op === 'delete') {
      const ref = refer(op.scene, at);
      if (ref.error) return ref.error;
      if (order.length <= 1) {
        return fail(`op ${at}: cannot delete scene ${op.scene} ("${ref.scene.name}"): a show needs at least one scene`);
      }
      commands.push(sceneCommands.delete(ref.scene.id, ref.scene.name));
      gone.add(ref.scene.id);
      order.splice(order.indexOf(ref.scene.id), 1);
      parts.push(`Delete scene ${op.scene} ("${ref.scene.name}")`);
    } else if (op.op === 'duplicate') {
      const ref = refer(op.scene, at);
      if (ref.error) return ref.error;
      const dup = sceneCommands.duplicate(ref.scene.id, ref.scene.name);
      commands.push(dup);
      order.splice(order.indexOf(ref.scene.id) + 1, 0, dup.copyId);
      parts.push(`Duplicate scene ${op.scene} ("${ref.scene.name}")`);
    } else if (op.op === 'move') {
      const ref = refer(op.scene, at);
      if (ref.error) return ref.error;
      if (!MOVE_POSITIONS.includes(op.position)) {
        return fail(`op ${at}: move position must be one of ${MOVE_POSITIONS.join(', ')}`, 'schema');
      }
      const relative = op.position === 'before' || op.position === 'after';
      let anchor = -1;
      let where = op.position === 'first' ? 'to the start' : 'to the end';
      if (relative) {
        if (!Number.isInteger(op.target)) return fail(`op ${at}: move ${op.position} needs a target scene number`, 'schema');
        const target = refer(op.target, at);
        if (target.error) return target.error;
        if (target.scene.id === ref.scene.id) {
          return fail(`op ${at}: scene ${op.scene} cannot be moved ${op.position} itself`, 'schema');
        }
        anchor = order.indexOf(target.scene.id);
        where = `${op.position} scene ${op.target}`;
      } else if (op.target !== undefined) {
        return fail(`op ${at}: move ${op.position} takes no target`, 'schema');
      }
      const from = order.indexOf(ref.scene.id);
      const to = moveTargetIndex(from, anchor, op.position, order.length);
      if (to === from) return fail(`op ${at}: scene ${op.scene} ("${ref.scene.name}") is already in that position`);
      commands.push(sceneCommands.reorder(from, to));
      order.splice(from, 1);
      order.splice(to, 0, ref.scene.id);
      parts.push(`Move scene ${op.scene} ("${ref.scene.name}") ${where}`);
    } else {
      return fail(`op ${at}: "${op.op}" is not a supported op (patch, add, move, delete or duplicate)`, 'schema');
    }
  }

  const summary = `Planner: ${parts.join('; ')}`;
  const command = sceneCommands.batch(commands, ids, sceneSignature(doc), summary);
  const dry = previewBatch(doc, command);
  return dry.ok ? { ok: true, command, summary, unsupported } : fail(dry.reason);
};
