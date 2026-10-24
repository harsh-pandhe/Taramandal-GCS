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
// tara_core DEFAULT_LIMITS.max_altitude_m.
const MAX_ALTITUDE_M = 120.0;

// Waypoints are exported rounded to 0.01 m per axis (round2), independently per
// drone. Each coordinate moves by at most half a step, so two drones' relative
// vector moves by at most 2 * 0.005 = EXPORT_STEP per axis, i.e. EXPORT_STEP *
// sqrt(3) in 3-D. Samples are taken at the same instants for every drone and
// linear interpolation keeps that bound between them. A pair whose exact separation is at least
// DESIGN_SEPARATION + QUANT_SLACK therefore cannot export below
// DESIGN_SEPARATION whatever the rounding does. The 2.05 m LAYER_CLEARANCE
// exceeds this bound (2.0173 m) with margin to spare.
const EXPORT_STEP = 0.01;
const QUANT_SLACK = EXPORT_STEP * Math.sqrt(3);
const DIRECT_CLEARANCE = DESIGN_SEPARATION + QUANT_SLACK;
// Two different jobs, kept apart on purpose:
//
//  * The FINAL gate in buildSequencedShow (and the exported-sample check that
//    decides direct vs layered) has NO tolerance. It measures the exported 1 cm
//    waypoints with the same strict comparison the backend validator makes
//    (tara_core.proximity.close_pairs rejects dist < 2.0, so exactly 2.0 passes
//    and 1.9999999999999982 does not). Waypoints sit on a 1 cm grid, so a pair
//    nominally 2.00 m apart can still compute a hair under 2.0; the backend
//    would reject it, so Studio must too.
//  * PLAN_EPS only absorbs ulp-level noise in the planner's closed-form
//    quadratics (minMorphSeparation) when it reasons about continuous straight
//    paths, e.g. pads exactly 2.0 m apart reading 1.9999999999999998. It never
//    accepts a show: whatever it lets through is still measured, strictly, on
//    the exported waypoints before buildSequencedShow returns.
const PLAN_EPS = 1e-9;

/**
 * A morph, or the finished show, cannot be built without breaking a safety
 * limit. Thrown deliberately (as opposed to an unexpected bug) so callers can
 * report the refusal to the operator and let every other error surface.
 */
export class MorphRefusalError extends Error {
  constructor(message, details = {}) {
    super(message);
    this.name = 'MorphRefusalError';
    Object.assign(this, details);
  }
}

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
const isPermutation = (a, n) => {
  if (!Array.isArray(a) || a.length !== n) return false;
  const seen = new Uint8Array(n);
  for (const k of a) {
    if (!Number.isInteger(k) || k < 0 || k >= n || seen[k]) return false;
    seen[k] = 1;
  }
  return true;
};

const normalizeAssignment = (targetIdx, sceneCount, n) => {
  if (targetIdx != null && !Array.isArray(targetIdx)) {
    throw new TypeError('targetIdx must be an array of permutations or null.');
  }
  const t = targetIdx ?? [];
  const shaped = t.length === sceneCount || t.length === sceneCount + 1;
  // The right shape but not a permutation of 0..n-1 is a corrupt assignment,
  // not a stale one: refuse it rather than fly an arbitrary mapping.
  const ok = (a) => {
    if (!shaped || !Array.isArray(a) || a.length !== n) return false;
    if (!isPermutation(a, n)) throw new TypeError('targetIdx entry is not a permutation of the fleet.');
    return true;
  };
  return {
    scenes: Array.from({ length: sceneCount }, (_, s) => (ok(t[s]) ? t[s] : identity(n))),
    ret: t.length === sceneCount + 1 && ok(t[sceneCount]) ? t[sceneCount] : identity(n),
  };
};

const minPairDist = (pts) => {
  let m = Infinity;
  for (let i = 0; i < pts.length; i++) {
    for (let j = i + 1; j < pts.length; j++) {
      const d = Math.hypot(pts[i].x - pts[j].x, pts[i].y - pts[j].y, (pts[i].z ?? 0) - (pts[j].z ?? 0));
      if (d < m) m = d;
    }
  }
  return m;
};

/**
 * Analytical minimum continuous pairwise separation over straight-line
 * interpolation between two point clouds `from` and `to`.
 * Evaluates the exact quadratic minimum on w in [0, 1] for every pair (i, j).
 */
export const minMorphSeparation = (from, to) => {
  const n = from.length;
  let minD = Infinity;
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      const ux = from[i].x - from[j].x, uy = from[i].y - from[j].y, uz = from[i].z - from[j].z;
      const u2 = ux * ux + uy * uy + uz * uz;
      const vx = (to[i].x - from[i].x) - (to[j].x - from[j].x);
      const vy = (to[i].y - from[i].y) - (to[j].y - from[j].y);
      const vz = (to[i].z - from[i].z) - (to[j].z - from[j].z);
      const v2 = vx * vx + vy * vy + vz * vz;
      const uv = ux * vx + uy * vy + uz * vz;
      let d2 = Math.min(u2, (ux + vx) * (ux + vx) + (uy + vy) * (uy + vy) + (uz + vz) * (uz + vz));
      if (v2 > 1e-12) {
        const wStar = -uv / v2;
        if (wStar > 0 && wStar < 1) {
          const critD2 = u2 - (uv * uv) / v2;
          if (critD2 < d2) d2 = critD2;
        }
      }
      const d = Math.sqrt(Math.max(0, d2));
      if (d < minD) minD = d;
    }
  }
  return minD;
};

/**
 * Pairs whose exact continuous separation over the straight-line morph is
 * below `limit`, plus the overall minimum. Same quadratic as minMorphSeparation.
 */
const morphPairsBelow = (from, to, limit) => {
  const n = from.length;
  let min = Infinity;
  const pairs = [];
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      const d = minMorphSeparation([from[i], from[j]], [to[i], to[j]]);
      if (d < min) min = d;
      if (d < limit) pairs.push([i, j]);
    }
  }
  return { min, pairs };
};

/**
 * Smallest separation, over all pairs, of the waypoints a straight eased morph
 * from `from` to `to` exports when sampled in `steps` steps (rounded as
 * buildSequencedShow rounds). Exposed so tests can measure the exported value.
 */
export const minExportedMorphSeparation = (from, to, steps) => {
  const pairs = [];
  for (let i = 0; i < from.length; i++) for (let j = i + 1; j < from.length; j++) pairs.push([i, j]);
  return exportedLegMin(from, to, pairs, steps);
};

/** Smallest distance from the origin to the segment a -> b (3-D). */
const originToSegment = (ax, ay, az, bx, by, bz) => {
  const dx = bx - ax, dy = by - ay, dz = bz - az;
  const dd = dx * dx + dy * dy + dz * dz;
  const w = dd < 1e-18 ? 0 : Math.max(0, Math.min(1, -(ax * dx + ay * dy + az * dz) / dd));
  return Math.hypot(ax + w * dx, ay + w * dy, az + w * dz);
};

/**
 * Exact minimum separation of the given pairs along the waypoints a leg will
 * actually export: every sample rounded exactly as buildSequencedShow rounds
 * it, joined by the straight segments the validator interpolates.
 */
const exportedLegMin = (from, to, pairs, steps) => {
  let min = Infinity;
  const at = (i, w) => ({
    x: round2(from[i].x + (to[i].x - from[i].x) * w),
    y: round2(from[i].y + (to[i].y - from[i].y) * w),
    z: round2(from[i].z + (to[i].z - from[i].z) * w),
  });
  for (const [i, j] of pairs) {
    let prev = null;
    for (let k = 0; k <= steps; k++) {
      const w = smooth(k / steps);
      const a = at(i, w), b = at(j, w);
      const rel = [a.x - b.x, a.y - b.y, a.z - b.z];
      if (prev) min = Math.min(min, originToSegment(...prev, ...rel));
      else min = Math.min(min, Math.hypot(...rel));
      prev = rel;
    }
  }
  return min;
};

/**
 * Exact minimum pairwise separation, and maximum altitude, of a built show's
 * exported waypoints (rounded values, linear between consecutive samples).
 * This measures what is actually exported, not the planner's internal geometry.
 * A uniform grid keeps it near-linear in the fleet size.
 *
 * @returns {{min:number, i:number, j:number, time:number, maxAltitude:number}}
 */
export const measureExportedGeometry = (drones) => {
  const n = drones.length;
  const out = { min: Infinity, i: -1, j: -1, time: 0, maxAltitude: 0 };
  if (!n) return out;
  const len = Math.min(...drones.map((d) => d.waypoints.length));
  const CELL = 4.0;
  for (let k = 0; k < len; k++) {
    for (let i = 0; i < n; i++) out.maxAltitude = Math.max(out.maxAltitude, -drones[i].waypoints[k].z);
  }
  const consider = (i, j, k) => {
    const a0 = drones[i].waypoints[k], a1 = drones[i].waypoints[Math.min(k + 1, len - 1)];
    const b0 = drones[j].waypoints[k], b1 = drones[j].waypoints[Math.min(k + 1, len - 1)];
    const d = originToSegment(
      a0.x - b0.x, a0.y - b0.y, a0.z - b0.z, a1.x - b1.x, a1.y - b1.y, a1.z - b1.z,
    );
    if (d < out.min) Object.assign(out, { min: d, i, j, time: a0.time });
  };
  for (let k = 0; k < len; k++) {
    const grid = new Map();
    const box = [];
    for (let i = 0; i < n; i++) {
      const a = drones[i].waypoints[k], b = drones[i].waypoints[Math.min(k + 1, len - 1)];
      const lo = [Math.min(a.x, b.x) - 1, Math.min(a.y, b.y) - 1, Math.min(a.z, b.z) - 1];
      const hi = [Math.max(a.x, b.x) + 1, Math.max(a.y, b.y) + 1, Math.max(a.z, b.z) + 1];
      box.push([lo, hi]);
      for (let cx = Math.floor(lo[0] / CELL); cx <= Math.floor(hi[0] / CELL); cx++) {
        for (let cy = Math.floor(lo[1] / CELL); cy <= Math.floor(hi[1] / CELL); cy++) {
          for (let cz = Math.floor(lo[2] / CELL); cz <= Math.floor(hi[2] / CELL); cz++) {
            const key = `${cx},${cy},${cz}`;
            if (!grid.has(key)) grid.set(key, []);
            grid.get(key).push(i);
          }
        }
      }
    }
    const done = new Set();
    for (const cell of grid.values()) {
      for (let x = 0; x < cell.length; x++) {
        for (let y = x + 1; y < cell.length; y++) {
          const i = Math.min(cell[x], cell[y]), j = Math.max(cell[x], cell[y]);
          const key = i * n + j;
          if (done.has(key)) continue;
          done.add(key);
          const [alo, ahi] = box[i], [blo, bhi] = box[j];
          if (alo[0] > bhi[0] || blo[0] > ahi[0] || alo[1] > bhi[1] || blo[1] > ahi[1]
            || alo[2] > bhi[2] || blo[2] > ahi[2]) continue;
          consider(i, j, k);
        }
      }
    }
  }
  return out;
};

/**
 * Layer index per drone for a layered morph (lift, transit at layer height,
 * descent), or null when no layering keeps every pair apart.
 *
 * - Two drones whose horizontal paths come within `clearance` get different
 *   layers, so their transit legs are at least a layer apart vertically.
 * - The lift and the descent are vertical. Two drones less than
 *   DESIGN_SEPARATION apart horizontally would pass through each other there
 *   if their up-down order changed, so such a pair keeps its starting order in
 *   the lift and takes its final order in the descent. A pair that would need
 *   both orders cannot be layered: null.
 *
 * Layers are assigned lowest first, each drone taking the lowest layer above
 * every drone it must clear and unused by any drone it conflicts with.
 */
export const computeMorphLayers = (from, to, clearance = LAYER_CLEARANCE) => {
  const n = from.length;
  const conflict = Array.from({ length: n }, () => []);
  const below = Array.from({ length: n }, () => new Set()); // drones i must be above
  const above = Array.from({ length: n }, () => []);
  const order = (pts, i, j) => {
    if (Math.hypot(pts[i].x - pts[j].x, pts[i].y - pts[j].y) >= DESIGN_SEPARATION) return;
    const [hi, lo] = pts[i].z < pts[j].z ? [i, j] : [j, i]; // NED: smaller z is higher
    if (!below[hi].has(lo)) { below[hi].add(lo); above[lo].push(hi); }
  };
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      const ax = from[i].x - from[j].x, ay = from[i].y - from[j].y;
      const dx = to[i].x - to[j].x - ax, dy = to[i].y - to[j].y - ay;
      const dd = dx * dx + dy * dy;
      const a = dd < 1e-12 ? 0 : Math.max(0, Math.min(1, -(ax * dx + ay * dy) / dd));
      if (Math.hypot(ax + a * dx, ay + a * dy) < clearance) {
        conflict[i].push(j);
        conflict[j].push(i);
      }
      order(from, i, j);
      order(to, i, j);
    }
  }
  const layer = new Array(n).fill(-1);
  const waiting = below.map((s) => s.size);
  const ready = identity(n).filter((i) => waiting[i] === 0);
  let done = 0;
  while (ready.length) {
    // Most-constrained first, then by index, so the result is deterministic.
    ready.sort((a, b) => conflict[b].length - conflict[a].length || a - b);
    const i = ready.shift();
    let c = 0;
    for (const lo of below[i]) c = Math.max(c, layer[lo] + 1);
    const taken = new Set(conflict[i].map((j) => layer[j]));
    while (taken.has(c)) c++;
    layer[i] = c;
    done++;
    for (const hi of above[i]) if (--waiting[hi] === 0) ready.push(hi);
  }
  return done === n ? layer : null; // fewer done = the order constraints form a cycle
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
// buildFormation reports its own refusals (spacing, site radius, ceiling) as
// plain Errors. Re-raise exactly those as MorphRefusalError so the UI can tell a
// deliberate refusal from a bug; anything else (TypeError, RangeError, ...) is
// left alone to surface.
const formationFor = (scene, n) => {
  const { shapeType, shapePayload, scale } = scene;
  try {
    return buildFormation(shapeType, shapePayload, n, scale);
  } catch (e) {
    if (e && e.constructor === Error) {
      throw new MorphRefusalError(`Scene "${scene.name}": ${e.message}`, { code: 'FORMATION', cause: e });
    }
    throw e;
  }
};

const planShow = (scenes, n, targetIdx, rate = null) => {
  const ground = Array.from({ length: n }, (_, i) => padAt(i));
  const hover = Array.from({ length: n }, (_, i) => hoverAt(i));
  const assignment = normalizeAssignment(targetIdx, scenes.length, n);
  // Plan on the 1 cm grid the waypoints are exported on, so endpoint spacing
  // here is the spacing the exported show has (round2 of a held position is
  // that position).
  const scenePos = scenes.map((s, k) => {
    const f = formationFor(s, n);
    return Array.from({ length: n }, (_, i) => {
      const p = f[assignment.scenes[k][i]];
      if (!p) throw new RangeError(`Formation "${s.shapePayload}" has no point ${assignment.scenes[k][i]} for ${n} drones.`);
      return { ...p, x: round2(p.x), y: round2(p.y), z: round2(p.z) };
    });
  });

  const legs = [];
  const move = (kind, label, sceneId, group, from, to, requested) => {
    const { dh, dv } = maxTravel(from, to);
    const direct = () => legs.push({ kind, label, sceneId, group, from, to, secs: easedSecs(requested, dh, dv) });
    if (kind !== 'morph') return direct();

    const { min, pairs } = morphPairsBelow(from, to, DIRECT_CLEARANCE);
    let needsLayers = min < DESIGN_SEPARATION - PLAN_EPS;
    if (!needsLayers && pairs.length) {
      // Exact separation clears the limit but not by the rounding bound: only
      // a direct morph whose exported waypoints are measured to stay at or
      // above the limit may fly. Without a sample rate there is nothing to
      // measure, so layer (the conservative choice).
      const r = rate && rate >= 1 ? rate : null;
      const steps = r ? legSteps({ kind, secs: easedSecs(requested, dh, dv) }, r) : 0;
      needsLayers = !r || exportedLegMin(from, to, pairs, steps) < DESIGN_SEPARATION;
    }
    if (!needsLayers) return direct();

    const minFrom = minPairDist(from);
    const minTo = minPairDist(to);
    if (minFrom < DESIGN_SEPARATION - PLAN_EPS || minTo < DESIGN_SEPARATION - PLAN_EPS) {
      throw new MorphRefusalError(`Morph "${label}" cannot be made collision-free: endpoint spacing is below minimum separation (${Math.min(minFrom, minTo).toFixed(2)}m < ${DESIGN_SEPARATION}m).`, { code: 'ENDPOINT_SPACING', label });
    }
    const layer = computeMorphLayers(from, to);
    if (!layer) {
      throw new MorphRefusalError(`Morph "${label}" cannot be made collision-free: two drones would have to swap their vertical order in the same column.`, { code: 'COLUMN_SWAP', label });
    }
    // A little over the design limit, so sampling and rounding cannot
    // shave a layered pair under it.
    const gap = LAYER_CLEARANCE;
    const maxLayer = Math.max(...layer);
    const highestAlt = Math.min(...from.map((p) => p.z), ...to.map((p) => p.z));
    const ceilingZ = -MAX_ALTITUDE_M;
    if (highestAlt - (maxLayer + 1) * gap < ceilingZ) {
      throw new MorphRefusalError(`Morph "${label}" cannot be made collision-free: required altitude layering exceeds ${MAX_ALTITUDE_M}m ceiling.`, { code: 'CEILING', label });
    }
    const fromUp = from.map((p, i) => ({ x: p.x, y: p.y, z: highestAlt - (layer[i] + 1) * gap }));
    const toUp = to.map((p, i) => ({ x: p.x, y: p.y, z: highestAlt - (layer[i] + 1) * gap }));

    // Every leg is checked exactly, not trusted: the layering is a plan,
    // and a plan that still brings a pair inside the limit is rejected.
    for (const [a, b] of [[from, fromUp], [fromUp, toUp], [toUp, to]]) {
      const legSep = minMorphSeparation(a, b);
      if (legSep < DESIGN_SEPARATION - PLAN_EPS) { // pads sit exactly 2.0 m apart
        throw new MorphRefusalError(`Morph "${label}" cannot be made collision-free: a layered leg comes within ${legSep.toFixed(2)}m.`, { code: 'LAYER_LEG', label });
      }
    }

    // 3 legs: lift, layered transit, descent
    const rLift = requested > 0 ? requested * 0.2 : 0;
    const rTransit = requested > 0 ? requested * 0.6 : 0;
    const rDesc = requested > 0 ? requested * 0.2 : 0;
    const t1 = maxTravel(from, fromUp);
    const t2 = maxTravel(fromUp, toUp);
    const t3 = maxTravel(toUp, to);
    legs.push({ kind, label, sceneId, group, from, to: fromUp, secs: easedSecs(rLift, t1.dh, t1.dv) });
    legs.push({ kind, label, sceneId, group, from: fromUp, to: toUp, secs: easedSecs(rTransit, t2.dh, t2.dv) });
    legs.push({ kind, label, sceneId, group, from: toUp, to, secs: easedSecs(rDesc, t3.dh, t3.dv) });
    return undefined;
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

/** Sum of the requested times: the length when no fleet plan is available. */
const nominalDuration = (scenes) => {
  const body = scenes.reduce(
    (sum, s) => sum + nonNeg(s.transitionDurationSeconds) + nonNeg(s.holdDurationSeconds), 0,
  );
  const nominalReturn = nonNeg(scenes[scenes.length - 1].transitionDurationSeconds);
  return round2(TAKEOFF_SECONDS + body + nominalReturn + LAND_SECONDS);
};

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

  if (!n || n < 1) return nominalDuration(scenes);

  const r = rate && rate >= 1 ? rate : null;
  const { legs } = planShow(scenes, n, targetIdx, r);
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
  const { legs, ground, assignment } = planShow(scenes, n, targetIdx, r);
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

  // Fail closed on what is actually exported (rounded waypoints, linear between
  // samples), not on the planner's internal geometry.
  const drones = wps.map((w, i) => ({ id: i, waypoints: w }));
  const geo = measureExportedGeometry(drones);
  if (geo.min < DESIGN_SEPARATION) {
    throw new MorphRefusalError(
      `Built show violates minimum separation: drones ${geo.i} and ${geo.j} come within ${geo.min.toFixed(3)}m (< ${DESIGN_SEPARATION}m) at t=${geo.time}s.`,
      { code: 'EXPORT_SEPARATION', drones: [geo.i, geo.j], separation: geo.min, time: geo.time },
    );
  }
  if (geo.maxAltitude > MAX_ALTITUDE_M) {
    throw new MorphRefusalError(
      `Built show exceeds the ${MAX_ALTITUDE_M}m altitude ceiling (${geo.maxAltitude.toFixed(2)}m).`,
      { code: 'EXPORT_CEILING', altitude: geo.maxAltitude },
    );
  }

  return {
    drones,
    _duration: round2(t),
    _minSeparationM: geo.min,
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
  ...scenes.map((s) => formationFor(s, n)),
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
const nominalTimelineSegments = (scenes, rate) => {
  const segs = [];
  let t = 0;
  const r = rate && rate >= 1 ? rate : null;
  const add = (kind, label, secs, sceneId) => {
    if (secs <= 0) return;
    segs.push({ kind, label, sceneId, start: round2(t), end: round2(t + secs) });
    t += secs;
  };
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
};

const plannedTimelineSegments = (legs, rate) => {
  const segs = [];
  let t = 0;
  const r = rate && rate >= 1 ? rate : null;
  // The return swap is several legs; the timeline shows it as one block.
  let i = 0;
  while (i < legs.length) {
    const head = legs[i];
    let secs = 0;
    while (i < legs.length && legs[i].group === head.group) {
      secs += r ? legSteps(legs[i], r) / r : legs[i].secs;
      i++;
    }
    if (secs > 0) {
      segs.push({ kind: head.kind, label: head.label, sceneId: head.sceneId, start: round2(t), end: round2(t + secs) });
      t += secs;
    }
  }
  return segs;
};

export const buildTimelineSegments = (scenes, n = null, rate = null, targetIdx = null) => {
  if (!n || n < 1 || !scenes.length) return nominalTimelineSegments(scenes, rate);
  const r = rate && rate >= 1 ? rate : null;
  return plannedTimelineSegments(planShow(scenes, n, targetIdx, r).legs, r);
};

/**
 * Duration and timeline for the UI, computed from ONE plan, without ever
 * throwing a safety refusal into a render.
 *
 * `basis` says what the numbers are:
 *   - 'planned': the real plan for `targetIdx`, identical to what
 *     buildSequencedShow will build for the same inputs.
 *   - 'nominal': the sum of the requested times. Used when there is no plan to
 *     measure: no fleet size, or no assignment yet (`targetIdx == null`; the
 *     flown plan depends on the optimal assignment, which is computed at build
 *     time, so an identity guess could be refused for a show that builds fine).
 *     Also used when the plan for a *stale* assignment is refused, in which
 *     case `refusal` carries the reason to show the operator.
 * Only MorphRefusalError is handled; any other error is a bug and propagates.
 *
 * @returns {{duration:number, segments:object[], basis:'planned'|'nominal', refusal:string|null}}
 */
export const predictShow = (scenes, n = null, rate = null, targetIdx = null) => {
  const nominal = (refusal = null) => ({
    duration: scenes.length ? nominalDuration(scenes) : 0,
    segments: nominalTimelineSegments(scenes, rate),
    basis: 'nominal',
    refusal,
  });
  if (!scenes.length) return nominal();
  if (!n || n < 1 || targetIdx == null) return nominal();
  const r = rate && rate >= 1 ? rate : null;
  try {
    const { legs } = planShow(scenes, n, targetIdx, r);
    const total = legs.reduce((sum, leg) => sum + (r ? legSteps(leg, r) / r : leg.secs), 0);
    return { duration: round2(total), segments: plannedTimelineSegments(legs, r), basis: 'planned', refusal: null };
  } catch (e) {
    if (e instanceof MorphRefusalError) return nominal(e.message);
    throw e;
  }
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
