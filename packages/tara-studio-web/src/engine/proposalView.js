// What the scene-edit proposal panel (issue #89) shows and when it may apply.
// Pure functions over plain data, so the component stays thin and the rules are
// tested without a DOM.

import { FORMATION_LABELS } from './formations.js';
import { batchRefusal, previewBatch } from './commands.js';

const num = (v) => (typeof v === 'number' ? `${Math.round(v * 100) / 100}` : String(v));

const shapeText = (scene) => {
  if (scene.shapeType === 'text') return `"${scene.shapePayload}"`;
  return FORMATION_LABELS[scene.shapePayload] ?? scene.shapePayload;
};

const SECONDS = {
  holdDurationSeconds: 'hold',
  transitionDurationSeconds: 'transition',
};

// One { label, value } row per visible change between two versions of a scene.
const sceneRows = (before, after, at) => {
  const rows = [];
  const who = `Scene ${at} "${before.name}"`;
  if (before.name !== after.name) rows.push({ label: `${who} name`, value: `"${before.name}" → "${after.name}"` });
  if (before.shapeType !== after.shapeType || before.shapePayload !== after.shapePayload) {
    rows.push({ label: `${who} shape`, value: `${shapeText(before)} → ${shapeText(after)}` });
  }
  for (const [key, name] of Object.entries(SECONDS)) {
    if (before[key] !== after[key]) rows.push({ label: `${who} ${name}`, value: `${num(before[key])} s → ${num(after[key])} s` });
  }
  if (before.scale !== after.scale) rows.push({ label: `${who} scale`, value: `${num(before.scale)}× → ${num(after.scale)}×` });
  if (before.transitLayerGapMeters !== after.transitLayerGapMeters) {
    rows.push({ label: `${who} transit gap`, value: `${num(before.transitLayerGapMeters)} m → ${num(after.transitLayerGapMeters)} m` });
  }
  return rows;
};

/**
 * Field-by-field list of what applying `command` to `doc` would change, as
 * [{ label, value }]. Empty when the command is refused. Derived from the
 * before/after documents, not from the command, so it cannot disagree with what
 * Apply does.
 */
export const describeChanges = (doc, command) => {
  const r = previewBatch(doc, command);
  if (!r.ok) return [];
  const before = doc.scenes;
  const after = r.doc.scenes;
  const rows = [];

  for (const [i, s] of before.entries()) {
    const next = after.find((x) => x.id === s.id);
    if (!next) rows.push({ label: `Scene ${i + 1} "${s.name}"`, value: 'deleted' });
    else rows.push(...sceneRows(s, next, i + 1));
  }
  for (const [j, s] of after.entries()) {
    if (!before.some((x) => x.id === s.id)) {
      rows.push({
        label: `New scene ${j + 1} "${s.name}"`,
        value: `${shapeText(s)}, hold ${num(s.holdDurationSeconds)} s, transition ${num(s.transitionDurationSeconds)} s`,
      });
    }
  }

  // Order: only the scenes present on both sides, compared by their rank.
  const common = after.filter((s) => before.some((x) => x.id === s.id)).map((s) => s.id);
  const was = before.filter((s) => common.includes(s.id)).map((s) => s.id);
  common.forEach((id, rank) => {
    if (was[rank] !== id) {
      const scene = after.find((s) => s.id === id);
      rows.push({
        label: `Scene "${scene.name}" position`,
        value: `${before.findIndex((s) => s.id === id) + 1} → ${after.findIndex((s) => s.id === id) + 1}`,
      });
    }
  });
  return rows;
};

const CATEGORY_NAMES = {
  proximity: 'Proximity', velocity: 'Velocity', acceleration: 'Acceleration',
  yaw_rate: 'Yaw rate', jerk: 'Jerk', altitude: 'Altitude', ground: 'Ground',
};

const violationText = (category, v) => {
  const at = typeof v?.time === 'number' ? ` at t = ${num(v.time)} s` : '';
  if (category === 'proximity' && Array.isArray(v?.drones)) return `Drones ${v.drones.join(' & ')} ${num(v.distance)} m apart${at}`;
  if (category === 'velocity' && v?.drone != null) return `Drone ${v.drone} at ${num(v.velocity)} m/s${at}`;
  return `${CATEGORY_NAMES[category] ?? category} violation${at}`;
};

/** Up to `max` readable lines for a backend violations object, plus a "more" line. */
export const violationLines = (violations, max = 5) => {
  const all = Object.entries(violations ?? {})
    .filter(([, list]) => Array.isArray(list))
    .flatMap(([category, list]) => list.map((v) => violationText(category, v)));
  if (all.length <= max) return all;
  return [...all.slice(0, max), `… and ${all.length - max} more`];
};

/**
 * Why Apply must stay disabled, or null when it may run. Checked at render time
 * and again inside the click handler. { kind, reason }:
 *   stale   - the scenes changed since the proposal was made (engine's own reason)
 *   context - drones, rate or pacing changed since the safety check ran
 *   check   - there is no PASSED safety check for exactly this proposal
 * `context` is any string that changes when the build inputs change.
 */
export const applyBlock = ({ doc, command, check, checkContext, context }) => {
  if (!command) return { kind: 'check', reason: 'Nothing to apply.' };
  const stale = batchRefusal(doc, command);
  if (stale) return { kind: 'stale', reason: stale };
  if (checkContext !== context) {
    return { kind: 'context', reason: 'Fleet size, rate or drone speed changed since the safety check - preview again.' };
  }
  if (!check || check.status === 'CHECKING') return { kind: 'check', reason: 'Safety check is still running.' };
  if (check.status !== 'PASSED') return { kind: 'check', reason: `Apply needs a PASSED safety check (this one is ${check.status}).` };
  return null;
};
