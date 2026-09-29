// Multi-scene show sequencer.
//
// Stitches an ordered list of SceneKeyframes into one continuous trajectory:
//
//   takeoff → [ transit → hold ]* → return → land
//
// and produces the standard {drones:[{id,waypoints:[{time,x,y,z,yaw}]}]}
// schema, so preview / validation / LED baking / export all consume it
// unchanged.
//
// @typedef {Object} SceneKeyframe
// @property {string} id
// @property {string} name
// @property {'parametric'|'text'|'svg'|'mesh'} shapeType
// @property {string} shapePayload              e.g. "circle" or "5"
// @property {number} scale
// @property {number} holdDurationSeconds       time parked in the formation
// @property {number} transitionDurationSeconds time morphing *into* it
// @property {number} transitLayerGapMeters     vertical spacing of the return layers
//
// Collision avoidance
// -------------------
// Every move between the hover grid and a formation, or between two
// formations, is a synchronized straight line under a minimum-total-squared-
// distance assignment. For such moves, if all start points and all goal points
// are at least sqrt(2)·d apart, no two drones ever come closer than d mid-move
// (Turpin, Michael & Kumar, "CAPT", 2014). The hover grid and every formation
// are spaced >= 3.2 m, so these moves stay >= 2.26 m apart — above the 2.0 m
// design threshold — with no altitude layering at all.
//
// The previous engine gave every drone its own 2 m altitude band during every
// morph. That span grows linearly with the fleet: at 100 drones it reached
// ~107 m and forced each morph to ~275 s to climb it, and it still breached
// separation at the start of the climb-out, which used the naive i → i mapping.
//
// The one move that can't use an optimal assignment is the final one: drone i
// has to land on its own pad i, which the GCS treats as its home. The return
// therefore goes to whichever hover spots the optimal assignment picks, then
// swaps drones onto their own pads by lifting each to a layer, crossing at
// that layer, and descending. Only drones whose crossing paths conflict get
// different layers, so a handful of layers are needed rather than one per drone.

import { buildFormation } from './formations.js';
import { SHOW_ALT, padAt, hoverAt } from './layout.js';
import { optimalAssignment } from './assignment.js';

export const TAKEOFF_SECONDS = 5.0;
export const LAND_SECONDS = 5.0;

// Default vertical layer spacing for the return swap. Must clear the STRICTER
// of the two safety limits in this pipeline: Studio's own validator historically
// used 0.8 m, but the GCS backend's pre-flight collision check (matching the
// live 10 Hz proximity monitor) uses 1.5 m — a real mismatch a dress rehearsal
// caught. The shared design threshold is now 2.0 m, so 2.0 m is also the floor.
export const DEFAULT_TRANSIT_GAP = 2.0;

const MAX_H = 3.2;   // horizontal velocity budget (m/s)
const MAX_V = 2.4;   // vertical velocity budget (m/s)
const MAX_ACC = 3.0; // acceleration budget for eased legs (m/s^2)

// tara_core DEFAULT_LIMITS.design_separation_m. Same-layer paths in the return
// swap are kept this far apart, plus a margin for 1 cm waypoint rounding.
const DESIGN_SEPARATION = 2.0;
const LAYER_CLEARANCE = DESIGN_SEPARATION + 0.05;

const smooth = (u) => u * u * (3 - 2 * u); // ease-in-out: zero velocity at ends
const round2 = (v) => parseFloat(v.toFixed(2));
const nonNeg = (v) => Math.max(0, v || 0);

// Every leg follows smoothstep: peak speed 1.5·D/T, peak acceleration 6·D/T².
const easedSecs = (secs, dh, dv) => Math.max(
  secs, (1.5 * dh) / MAX_H, (1.5 * dv) / MAX_V, Math.sqrt((6 * Math.hypot(dh, dv)) / MAX_ACC),
);

/** Largest horizontal and vertical distance any drone covers between two sets. */
const maxTravel = (from, to) => {
  let dh = 0, dv = 0;
  for (let i = 0; i < from.length; i++) {
    dh = Math.max(dh, Math.hypot(from[i].x - to[i].x, from[i].y - to[i].y));
    dv = Math.max(dv, Math.abs(from[i].z - to[i].z));
  }
  return { dh, dv };
};

export const makeScene = (patch = {}) => ({
  id: `scene-${Math.random().toString(36).slice(2, 10)}`,
  name: 'Scene',
  shapeType: 'parametric',
  shapePayload: 'circle',
  scale: 1.0,
  holdDurationSeconds: 6,
  transitionDurationSeconds: 4,
  transitLayerGapMeters: DEFAULT_TRANSIT_GAP,
  ...patch,
});

const identity = (n) => Array.from({ length: n }, (_, i) => i);

/**
 * targetIdx is `scenes.length` permutations (scene s: drone i occupies point
 * targetIdx[s][i]), optionally followed by one more for the return: the hover
 * spot each drone flies back to. Anything that doesn't fit this shape — e.g. an
 * assignment left over from a different scene list or fleet size — falls back
 * to identity rather than being misread.
 */
const normalizeAssignment = (targetIdx, sceneCount, n) => {
  const t = Array.isArray(targetIdx) ? targetIdx : [];
  const shaped = t.length === sceneCount || t.length === sceneCount + 1;
  const ok = (a) => shaped && Array.isArray(a) && a.length === n;
  return {
    scenes: Array.from({ length: sceneCount }, (_, s) => (ok(t[s]) ? t[s] : identity(n))),
    ret: t.length === sceneCount + 1 && ok(t[sceneCount]) ? t[sceneCount] : identity(n),
  };
};

/**
 * Layer index for each drone in the return swap: greedy colouring of the graph
 * whose edges are pairs whose synchronized horizontal paths come within
 * LAYER_CLEARANCE. Same-layer drones therefore never get too close, and
 * different layers are a full layer gap apart vertically.
 */
const returnLayers = (spots, homes) => {
  const n = spots.length;
  const adj = Array.from({ length: n }, () => []);
  const still = spots.map((p, i) => p.x === homes[i].x && p.y === homes[i].y);
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      // Two drones that don't move sit exactly on their pads' spacing. Two that
      // move — even in lockstep, 2.0 m apart — are rounded to the centimetre
      // independently every sample and can read 1.99 m, so they need the margin.
      if (still[i] && still[j]) continue;
      const ax = spots[i].x - spots[j].x, ay = spots[i].y - spots[j].y;
      const dx = homes[i].x - homes[j].x - ax, dy = homes[i].y - homes[j].y - ay;
      const dd = dx * dx + dy * dy;
      const a = dd < 1e-12 ? 0 : Math.max(0, Math.min(1, -(ax * dx + ay * dy) / dd));
      if (Math.hypot(ax + a * dx, ay + a * dy) < LAYER_CLEARANCE) {
        adj[i].push(j);
        adj[j].push(i);
      }
    }
  }
  const order = identity(n).sort((a, b) => adj[b].length - adj[a].length || a - b);
  const layer = new Array(n).fill(-1);
  for (const i of order) {
    const taken = new Set(adj[i].map((j) => layer[j]));
    let c = 0;
    while (taken.has(c)) c++;
    layer[i] = c;
  }
  return layer;
};

/**
 * The show as a list of legs, each an interpolation between two position sets
 * over a paced duration. Building, predicting the duration and drawing the
 * timeline all read this one plan, so they cannot disagree.
 */
const planShow = (scenes, n, targetIdx) => {
  const ground = Array.from({ length: n }, (_, i) => padAt(i));
  const hover = Array.from({ length: n }, (_, i) => hoverAt(i));
  const assignment = normalizeAssignment(targetIdx, scenes.length, n);
  const scenePos = scenes.map((s, k) => {
    const f = buildFormation(s.shapeType, s.shapePayload, n, s.scale);
    return Array.from({ length: n }, (_, i) => f[assignment.scenes[k][i]] ?? f[i]);
  });

  const legs = [];
  const move = (kind, label, sceneId, group, from, to, requested) => {
    const { dh, dv } = maxTravel(from, to);
    legs.push({ kind, label, sceneId, group, from, to, secs: easedSecs(requested, dh, dv) });
  };

  // Vertical take-off over each drone's own pad: no horizontal motion, so pairs
  // never get closer than their pad spacing.
  move('takeoff', 'Takeoff', null, 'takeoff', ground, hover, TAKEOFF_SECONDS);

  let prev = hover;
  scenes.forEach((s, i) => {
    move('morph', i === 0 ? 'Climb-out' : `Morph → ${s.name}`, s.id, `morph-${i}`,
      prev, scenePos[i], nonNeg(s.transitionDurationSeconds));
    legs.push({
      kind: 'hold', label: s.name, sceneId: s.id, group: `hold-${i}`,
      from: scenePos[i], to: scenePos[i], secs: nonNeg(s.holdDurationSeconds),
    });
    prev = scenePos[i];
  });

  const last = scenes[scenes.length - 1];
  const spots = assignment.ret.map((k) => hover[k]);
  move('morph', 'Return', last.id, 'return', prev, spots, nonNeg(last.transitionDurationSeconds));

  if (assignment.ret.some((k, i) => k !== i)) {
    const gap = Math.max(DESIGN_SEPARATION, Number.isFinite(last.transitLayerGapMeters)
      ? last.transitLayerGapMeters : DEFAULT_TRANSIT_GAP);
    const layer = returnLayers(spots, hover);
    const at = (pts) => pts.map((p, i) => ({ x: p.x, y: p.y, z: -(SHOW_ALT + gap * layer[i]) }));
    const spotsUp = at(spots), homesUp = at(hover);
    // Lift and descent are vertical over distinct pads, so they can't close a
    // pair below its pad spacing; the crossing happens only at layer height.
    move('morph', 'Return', last.id, 'return', spots, spotsUp, 0);
    move('morph', 'Return', last.id, 'return', spotsUp, homesUp, 0);
    move('morph', 'Return', last.id, 'return', homesUp, hover, 0);
  }

  move('land', 'Landing', null, 'land', hover, ground, LAND_SECONDS);
  return { legs, ground, assignment };
};

// Every leg below is emitted as a HALF-OPEN interval: it pushes its starting
// sample and every sample up to but NOT including its endpoint, then advances
// the clock by exactly its own duration. The next leg's first sample is that
// endpoint. One closing sample is pushed after the last leg.
//
// This is what makes a 6 s hold occupy exactly 6 s of show time. An earlier
// implementation emitted the endpoint too, so each leg re-sent the position the
// previous leg had already ended on and the clock gained an extra dt per leg.
const legSteps = (leg, r) => (leg.kind === 'hold'
  ? Math.max(0, Math.round(leg.secs * r))
  : Math.max(1, Math.round(leg.secs * r)));

/**
 * Total wall-clock length of a show, without building it.
 *
 * `n` is optional: without it this returns the nominal sum of the requested
 * times (takeoff, each scene's morph-in and hold, a return leg as long as the
 * last morph, and landing). Pass the fleet size (and the sample rate, which
 * quantizes every leg to whole samples) to get the real duration, which also
 * accounts for legs the velocity budget forces to run slower than requested.
 * With both, and the same `targetIdx`, this equals the built show's `_duration`
 * exactly.
 */
export const computeShowDuration = (scenes, n = null, rate = null, targetIdx = null) => {
  if (!scenes.length) return 0;

  if (!n || n < 1) {
    const body = scenes.reduce(
      (sum, s) => sum + nonNeg(s.transitionDurationSeconds) + nonNeg(s.holdDurationSeconds), 0,
    );
    const nominalReturn = nonNeg(scenes[scenes.length - 1].transitionDurationSeconds);
    return round2(TAKEOFF_SECONDS + body + nominalReturn + LAND_SECONDS);
  }

  const r = rate && rate >= 1 ? rate : null;
  const { legs } = planShow(scenes, n, targetIdx);
  const total = legs.reduce((sum, leg) => sum + (r ? legSteps(leg, r) / r : leg.secs), 0);
  return round2(total);
};

/**
 * Builds the full trajectory for a scene list.
 *
 * @param {SceneKeyframe[]} scenes
 * @param {number} n      fleet size
 * @param {number} rate   sample rate (Hz)
 * @param {number[][]|null} targetIdx  collision-free assignment from
 *        computeAssignmentChain. Defaults to identity (drone i → point i), the
 *        naive mapping, which is only collision-free by luck.
 * @returns {{drones: Array, _duration: number, _targetIdx: number[][]}}
 */
export const buildSequencedShow = (scenes, n, rate, targetIdx = null) => {
  if (!scenes.length || n < 1) return { drones: [], _duration: 0 };

  const r = Math.max(1, rate), dt = 1 / r;
  const { legs, ground, assignment } = planShow(scenes, n, targetIdx);
  const wps = Array.from({ length: n }, () => []);
  let t = 0;

  const push = (positions) => {
    for (let i = 0; i < n; i++) {
      const p = positions[i];
      wps[i].push({ time: round2(t), x: round2(p.x), y: round2(p.y), z: round2(p.z), yaw: 0 });
    }
  };

  for (const leg of legs) {
    const steps = legSteps(leg, r);
    for (let k = 0; k < steps; k++) {
      const w = smooth(k / steps); // a hold's from and to are the same points
      push(leg.from.map((a, i) => ({
        x: a.x + (leg.to[i].x - a.x) * w,
        y: a.y + (leg.to[i].y - a.y) * w,
        z: a.z + (leg.to[i].z - a.z) * w,
      })));
      t += dt;
    }
  }
  push(ground); // closing sample — the show ends parked on the pads

  return {
    drones: wps.map((w, i) => ({ id: i, waypoints: w })),
    _duration: round2(t),
    // Kept on the show so the UI can predict durations and timeline blocks for
    // exactly this assignment (see App.jsx).
    _targetIdx: [...assignment.scenes, assignment.ret],
  };
};

// The chain runs hover grid → each scene → hover grid. Starting from the hover
// grid makes the climb-out an optimal assignment too (the backend's chain
// otherwise starts every drone on its own index of the first formation), and
// ending on it picks the return spots.
const chainForms = (scenes, n) => [
  Array.from({ length: n }, (_, i) => hoverAt(i)),
  ...scenes.map((s) => buildFormation(s.shapeType, s.shapePayload, n, s.scale)),
  Array.from({ length: n }, (_, i) => hoverAt(i)),
];

/**
 * Computes the collision-free assignment chain across a scene list in ONE
 * request to /api/assign-chain, rather than one request per consecutive pair.
 *
 * @returns {Promise<number[][]>} targetIdx: one permutation per scene, then the
 *          return spots
 * @throws if the backend is unreachable — the caller falls back to
 *         computeAssignmentChainLocal.
 */
export const computeAssignmentChain = async (scenes, n, apiBase) => {
  const resp = await fetch(`${apiBase}/api/assign-chain`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ forms: chainForms(scenes, n) }),
  });
  if (!resp.ok) throw new Error(`assign-chain failed (${resp.status})`);
  const data = await resp.json();
  return data.target_idx.slice(1); // [0] is the hover grid itself: identity
};

/** Same chain as computeAssignmentChain, solved in the browser. */
export const computeAssignmentChainLocal = (scenes, n) => {
  const forms = chainForms(scenes, n);
  const chain = [identity(n)];
  for (let s = 0; s < forms.length - 1; s++) {
    const from = chain[s].map((k) => forms[s][k]);
    chain.push(optimalAssignment(from, forms[s + 1]));
  }
  return chain.slice(1);
};

/**
 * Timeline segments for the scrubber's multi-layer track: one entry per leg
 * with its wall-clock span, so the UI can draw scene blocks against real time
 * and map a scrub position back to the scene under the playhead.
 *
 * Takes the fleet size (and assignment) for the same reason computeShowDuration
 * does — a leg the velocity budget slows down occupies more of the timeline than
 * the time the user typed, and the blocks have to line up with the trajectory
 * the viewport is actually playing.
 */
export const buildTimelineSegments = (scenes, n = null, rate = null, targetIdx = null) => {
  const segs = [];
  let t = 0;
  const r = rate && rate >= 1 ? rate : null;

  const add = (kind, label, secs, sceneId) => {
    if (secs <= 0) return;
    segs.push({ kind, label, sceneId, start: round2(t), end: round2(t + secs) });
    t += secs;
  };

  if (!n || n < 1 || !scenes.length) {
    const quantMorph = (secs) => (r ? Math.max(1, Math.round(secs * r)) / r : secs);
    const quantLeg = (secs) => (r ? Math.max(0, Math.round(secs * r)) / r : secs);
    add('takeoff', 'Takeoff', quantMorph(TAKEOFF_SECONDS), null);
    scenes.forEach((s, i) => {
      add('morph', i === 0 ? 'Climb-out' : `Morph → ${s.name}`, quantMorph(nonNeg(s.transitionDurationSeconds)), s.id);
      add('hold', s.name, quantLeg(nonNeg(s.holdDurationSeconds)), s.id);
    });
    if (scenes.length) {
      const lastScene = scenes[scenes.length - 1];
      add('morph', 'Return', quantMorph(nonNeg(lastScene.transitionDurationSeconds)), lastScene.id);
    }
    add('land', 'Landing', quantMorph(LAND_SECONDS), null);
    return segs;
  }

  const { legs } = planShow(scenes, n, targetIdx);
  // The return swap is several legs; the timeline shows it as one block.
  let i = 0;
  while (i < legs.length) {
    const head = legs[i];
    let secs = 0;
    while (i < legs.length && legs[i].group === head.group) {
      secs += r ? legSteps(legs[i], r) / r : legs[i].secs;
      i++;
    }
    add(head.kind, head.label, secs, head.sceneId);
  }
  return segs;
};

/** Linear interpolation of a drone's waypoint list at time `t`. */
export const interpolateWaypoint = (waypoints, t) => {
  if (!waypoints || waypoints.length === 0) return null;
  if (t <= waypoints[0].time) return waypoints[0];
  const last = waypoints[waypoints.length - 1];
  if (t >= last.time) return last;

  // Waypoints are uniformly sampled, so jump straight to the bracketing pair
  // instead of scanning. This runs per drone per frame — at fleet scale the
  // linear scan was the preview's hot loop.
  const step = waypoints.length > 1 ? waypoints[1].time - waypoints[0].time : 1;
  let i = step > 0 ? Math.floor((t - waypoints[0].time) / step) : 0;
  i = Math.max(0, Math.min(waypoints.length - 2, i));
  while (i > 0 && waypoints[i].time > t) i--;
  while (i < waypoints.length - 2 && waypoints[i + 1].time <= t) i++;

  const w1 = waypoints[i], w2 = waypoints[i + 1];
  const span = w2.time - w1.time;
  const ratio = span > 0 ? (t - w1.time) / span : 0;
  return {
    x: w1.x + ratio * (w2.x - w1.x),
    y: w1.y + ratio * (w2.y - w1.y),
    z: w1.z + ratio * (w2.z - w1.z),
    yaw: w1.yaw + ratio * (w2.yaw - w1.yaw),
  };
};
