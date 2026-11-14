// Compiles a local-planner draft (issue #94, slice 1) into ONE pinned scene/batch,
// or refuses with a reason. The planner's ops are scene NUMBERS and plain field
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

/** The backend accepts at most this many scenes and ops (tara_studio/ai_plan.py). */
export const MAX_PLAN_SCENES = 50;
export const MAX_PLAN_OPS = 3;

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
    return fail(
      unsupported.length
        ? `not supported: ${unsupported.join('; ')}. The planner only edits scene shapes, sizes, timing and names.`
        : 'the planner proposed no edit',
      'unsupported',
    );
  }
  if (ops.length > MAX_PLAN_OPS) return fail(`at most ${MAX_PLAN_OPS} ops are allowed, got ${ops.length}`, 'schema');

  const ids = doc.scenes.map((s) => s.id);
  const commands = [];
  const parts = [];

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
      if (!Number.isInteger(op.scene) || op.scene < 1 || op.scene > doc.scenes.length) {
        return fail(`op ${at}: scene ${op.scene} does not exist (the show has ${doc.scenes.length})`, 'schema');
      }
      if (Object.keys(fields).length === 0) return fail(`op ${at}: patch of scene ${op.scene} changes no field`, 'schema');
      const scene = doc.scenes[op.scene - 1];
      commands.push(sceneCommands.patch(scene.id, fields));
      parts.push(`Edit scene ${op.scene} ("${scene.name}"): ${describeFields(fields)}`);
    } else if (op.op === 'add') {
      if (!hasShape) return fail(`op ${at}: add needs a shape`, 'schema');
      // Only the fields that were set: an undefined key would override makeScene's default.
      commands.push(sceneCommands.add(fields));
      parts.push(`Add a ${shapeLabel(fields.shapeType, fields.shapePayload)} scene${fields.name ? ` "${fields.name}"` : ''}`);
    } else {
      return fail(`op ${at}: "${op.op}" is not a supported op (patch or add)`, 'schema');
    }
  }

  const summary = `Planner: ${parts.join('; ')}`;
  const command = sceneCommands.batch(commands, ids, sceneSignature(doc), summary);
  const dry = previewBatch(doc, command);
  return dry.ok ? { ok: true, command, summary, unsupported } : fail(dry.reason);
};
