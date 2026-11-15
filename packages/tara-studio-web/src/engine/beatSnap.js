// Snap scene ends to musical beats (issue #274, music work of #91).
//
// Pure: no React, no clock, no throws on odd input. Only a scene's hold
// (holdDurationSeconds) is ever changed; the transition is never touched, so the
// morph into each scene keeps its time and only the dwell stretches or shrinks.
// The result is one scene/batch, so the whole snap is a single undo step.

import { SCENE_LIMITS } from './sceneLimits.js';
import { sceneCommands, previewBatch } from './commands.js';
import { sceneSignature } from './sceneValidation.js';

export const MAX_SNAP_WINDOW_S = 0.75;
export const MIN_SNAPPED_HOLD_S = 1.0;

const round2 = (x) => Math.round(x * 100) / 100;
const isNum = (x) => typeof x === 'number' && Number.isFinite(x);

/** Beat times, ascending, from {t, confidence} objects or plain numbers. Junk is dropped. */
export const beatTimes = (beats) => {
  if (!Array.isArray(beats)) return [];
  return beats
    .map((b) => (typeof b === 'number' ? b : b?.t))
    .filter(isNum)
    .sort((a, b) => a - b);
};

/** min(0.75 s, half the median beat interval); 0.75 s when there is no interval to measure. */
export const snapWindow = (times) => {
  const gaps = [];
  for (let i = 1; i < times.length; i += 1) {
    const g = times[i] - times[i - 1];
    if (g > 0) gaps.push(g);
  }
  if (!gaps.length) return MAX_SNAP_WINDOW_S;
  gaps.sort((a, b) => a - b);
  const mid = Math.floor(gaps.length / 2);
  const median = gaps.length % 2 ? gaps[mid] : (gaps[mid - 1] + gaps[mid]) / 2;
  return Math.min(MAX_SNAP_WINDOW_S, median / 2);
};

const nearest = (times, t) => {
  let best = null;
  for (const b of times) if (best === null || Math.abs(b - t) < Math.abs(best - t)) best = b;
  return best;
};

/**
 * @param {object} args
 * @param {object[]} args.scenes   the scenes, in order
 * @param {object[]} args.segments timeline segments (buildTimelineSegments) - the real
 *   times, takeoff and sample-rate rounding included
 * @param {Array<number|{t:number}>} args.beats
 * @param {{min:number,max:number}} [args.limits] allowed hold range
 * @param {number} [args.rate] sample rate (Hz) the timeline was built at; holds then snap to whole samples
 * @returns {{patches: {id:string, holdDurationSeconds:number}[], aligned: number,
 *   skipped: {id:string, reason:string}[]}}
 */
export const planBeatSnap = ({ scenes, segments, beats, rate, limits = SCENE_LIMITS.holdDurationSeconds } = {}) => {
  const patches = [];
  const skipped = [];
  let aligned = 0;
  const list = Array.isArray(scenes) ? scenes : [];
  const times = beatTimes(beats);
  if (!times.length) {
    for (const s of list) skipped.push({ id: s?.id, reason: 'no beats' });
    return { patches, aligned, skipped };
  }
  const window = snapWindow(times);
  const segs = Array.isArray(segments) ? segments : [];
  // The timeline plays a hold as a whole number of samples (sequencer.js legSteps), so the
  // end that lands on the beat is that of the sample-rounded hold, not of the typed one.
  const r = isNum(rate) && rate >= 1 ? rate : null;
  const played = (secs) => (r ? Math.max(0, Math.round(secs * r)) / r : secs);
  // Changing a scene's hold moves everything after it by the same amount.
  let shift = 0;

  for (const scene of list) {
    const id = scene?.id;
    const hold = scene?.holdDurationSeconds;
    const seg = segs.find((g) => g?.sceneId === id && g?.kind === 'hold');
    if (!seg || !isNum(seg.end) || !isNum(hold)) {
      skipped.push({ id, reason: 'no timeline position' });
      continue;
    }
    const rawEnd = seg.end + shift;
    const target = nearest(times, rawEnd);
    if (target === null || Math.abs(target - rawEnd) > window) {
      skipped.push({ id, reason: 'no beat within the snap window' });
      continue;
    }
    const wanted = played(hold) + (target - rawEnd);
    const newHold = r ? Math.round(Math.round(wanted * r) / r * 1e4) / 1e4 : round2(wanted);
    if (Math.abs(played(newHold) - played(hold)) < 1e-9) {
      aligned += 1;
      continue;
    }
    if (newHold < MIN_SNAPPED_HOLD_S) {
      skipped.push({ id, reason: `snapped hold ${newHold} s is under ${MIN_SNAPPED_HOLD_S} s` });
      continue;
    }
    if (newHold < limits.min || newHold > limits.max) {
      skipped.push({ id, reason: `snapped hold ${newHold} s is outside ${limits.min} to ${limits.max} s` });
      continue;
    }
    patches.push({ id, holdDurationSeconds: newHold });
    shift += played(newHold) - played(hold);
  }
  return { patches, aligned, skipped };
};

/** The plan as one all-or-nothing, single-undo-step command pinned to `doc`; null when nothing changes. */
export const buildSnapBatch = (doc, plan) => {
  if (!plan?.patches?.length || !Array.isArray(doc?.scenes)) return null;
  return sceneCommands.batch(
    plan.patches.map((p) => sceneCommands.patch(p.id, { holdDurationSeconds: p.holdDurationSeconds })),
    doc.scenes.map((s) => s.id),
    sceneSignature(doc),
    'Snap scenes to beats',
  );
};

const plural = (n) => `${n} scene${n === 1 ? '' : 's'}`;

/**
 * Plan, dry-run and describe a snap for the current document.
 * @returns {{command: object|null, message: string, resultDoc: object}} `command` is ready for
 *   history.execute; `resultDoc` is the document once it has run (`doc` itself when nothing changes)
 */
export const runBeatSnap = ({ doc, segments, analysis, rate }) => {
  const times = beatTimes(analysis?.beats);
  if (!times.length) {
    return { command: null, message: 'No tempo or beats were found in this track, so there is nothing to snap to.', resultDoc: doc };
  }
  if (!Array.isArray(segments) || !segments.length) {
    return { command: null, message: 'The timeline is not available (the scenes cannot be laid out yet), so nothing was changed.', resultDoc: doc };
  }
  const plan = planBeatSnap({ scenes: doc?.scenes, segments, beats: times, rate });
  const summary = `Snapped ${plural(plan.patches.length)}, ${plan.aligned} already on a beat, ${plan.skipped.length} skipped`;
  const command = buildSnapBatch(doc, plan);
  if (!command) return { command: null, message: summary, resultDoc: doc };
  const dry = previewBatch(doc, command);
  if (!dry.ok) return { command: null, message: `Snap refused, nothing was changed: ${dry.reason}`, resultDoc: doc };
  return { command, message: summary, resultDoc: dry.doc };
};
