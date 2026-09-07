// Multi-scene show sequencer.
//
// Stitches an ordered list of SceneKeyframes into one continuous trajectory:
//
//   takeoff → [ transit → hold ]* → land
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
// @property {number} transitLayerGapMeters     vertical spacing during that morph

import { buildFormation, SHOW_ALT } from './formations.js';

export const TAKEOFF_SECONDS = 5.0;
export const LAND_SECONDS = 5.0;

// Default vertical layer spacing during a morph. Must clear the STRICTER of the
// two safety limits in this pipeline: Studio's own validator uses 0.8 m, but the
// GCS backend's pre-flight collision check (matching the live 10 Hz proximity
// monitor) uses 1.5 m — a real mismatch a dress rehearsal caught, where a show
// that passed Studio's check was still rejected at GCS upload. 2.0 m clears
// 1.5 m with margin.
export const DEFAULT_TRANSIT_GAP = 2.0;

const MAX_H = 3.2; // horizontal velocity budget (m/s)
const MAX_V = 2.4; // vertical velocity budget (m/s)

// Fraction of a morph spent ramping the transit lift up/down. A short ramp
// reaches full vertical separation quickly, minimizing time at partial lift
// where horizontal closing distance can combine with incomplete vertical
// separation to breach the safety limit — exactly what a dress rehearsal caught
// at the takeoff→first-formation morph (min separation dipped to ~1.4 m against
// the 1.5 m GCS limit).
const TRANSIT_RAMP = 0.15;

const smooth = (u) => u * u * (3 - 2 * u); // ease-in-out: zero velocity at ends
const round2 = (v) => parseFloat(v.toFixed(2));

/** Ground pad for drone i — a single row, 2 m apart. */
const padAt = (i) => ({ x: 0, y: 2 * i, z: 0 });

/**
 * The time a morph actually takes, which is the requested time unless that
 * would break the velocity budget.
 *
 * Two limits apply. The horizontal one scales with how far the furthest drone
 * has to travel. The vertical one comes from transit layering: giving N drones
 * pairwise vertical separation needs a total altitude span of gap*(N-1), so the
 * outermost drone lifts gap*(N-1)/2 — and that lift has to happen within the
 * ramp fraction of the morph. That term grows linearly with fleet size, so
 * beyond a handful of drones it, not the user's requested time, sets the pace.
 * Callers that display a duration must use this rather than summing the
 * requested times, or the UI will promise a show length the build won't honour.
 */
const pacedMorphSeconds = (secs, maxTravel, n, gapMeters) => {
  const gap = Number.isFinite(gapMeters) && gapMeters > 0 ? gapMeters : DEFAULT_TRANSIT_GAP;
  const maxLift = (gap * (n - 1)) / 2;
  return Math.max(secs, maxTravel / MAX_H, maxLift / (TRANSIT_RAMP * MAX_V));
};

/** Largest horizontal distance any drone travels between two point sets. */
const maxTravelBetween = (from, to) =>
  from.reduce((m, a, i) => Math.max(m, Math.hypot(a.x - to[i].x, a.y - to[i].y)), 0);

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

/**
 * Total wall-clock length of a show, without building it.
 *
 * Every scene contributes its morph-in plus its hold; takeoff and landing
 * bracket the whole thing. The first scene's morph-in is the climb-out from the
 * pad hover, and the landing is preceded by a return leg the same length as the
 * last scene's morph, so the fleet always comes back over its own pads before
 * descending.
 *
 * `n` is optional: without it this returns the nominal sum of the requested
 * times. Pass the fleet size (and the sample rate, which quantizes every leg to
 * whole samples) to get the real duration, which also accounts for morphs the
 * velocity budget forces to run slower than requested. With both, this equals
 * the built show's `_duration` exactly.
 */
export const computeShowDuration = (scenes, n = null, rate = null) => {
  if (!scenes.length) return 0;

  // Without a fleet size there is no geometry to pace against; report the
  // requested times as-is.
  if (!n || n < 1) {
    const body = scenes.reduce(
      (sum, s) => sum + Math.max(0, s.transitionDurationSeconds || 0) + Math.max(0, s.holdDurationSeconds || 0),
      0,
    );
    const nominalReturn = Math.max(0, scenes[scenes.length - 1].transitionDurationSeconds || 0);
    return round2(TAKEOFF_SECONDS + body + nominalReturn + LAND_SECONDS);
  }

  const padHover = Array.from({ length: n }, (_, i) => ({ x: 0, y: 2 * i, z: -SHOW_ALT }));
  const scenePos = scenes.map((s) => buildFormation(s.shapeType, s.shapePayload, n, s.scale));

  // Mirror the build's quantization: each leg runs a whole number of samples.
  const r = rate && rate >= 1 ? rate : null;
  const quantMorph = (secs) => (r ? Math.max(1, Math.round(secs * r)) / r : secs);
  const quantLeg = (secs) => (r ? Math.max(0, Math.round(secs * r)) / r : secs);

  let total = quantMorph(TAKEOFF_SECONDS) + quantMorph(LAND_SECONDS);
  for (let s = 0; s < scenes.length; s++) {
    const from = s === 0 ? padHover : scenePos[s - 1];
    total += quantMorph(pacedMorphSeconds(
      Math.max(0, scenes[s].transitionDurationSeconds || 0),
      maxTravelBetween(from, scenePos[s]), n, scenes[s].transitLayerGapMeters,
    ));
    total += quantLeg(Math.max(0, scenes[s].holdDurationSeconds || 0));
  }
  const lastScene = scenes[scenes.length - 1];
  total += quantMorph(pacedMorphSeconds(
    Math.max(0, lastScene.transitionDurationSeconds || 0),
    maxTravelBetween(scenePos[scenePos.length - 1], padHover), n, lastScene.transitLayerGapMeters,
  ));
  return round2(total);
};

/**
 * Builds the full trajectory for a scene list.
 *
 * @param {SceneKeyframe[]} scenes
 * @param {number} n      fleet size
 * @param {number} rate   sample rate (Hz)
 * @param {number[][]|null} targetIdx  collision-free assignment: targetIdx[s][i]
 *        is the index in scene s's point cloud that drone i occupies. Defaults
 *        to identity (drone i → point i), the naive mapping that can make
 *        morphs cross.
 * @returns {{drones: Array, _duration: number}}
 */
export const buildSequencedShow = (scenes, n, rate, targetIdx = null) => {
  if (!scenes.length || n < 1) return { drones: [], _duration: 0 };

  const r = Math.max(1, rate), dt = 1 / r;
  const ground = Array.from({ length: n }, (_, i) => padAt(i));

  // A hover point directly above each pad. Drones take off and land purely
  // vertically over their own pad (no horizontal motion → no crossing); all
  // horizontal repositioning happens as layered morphs at altitude.
  const padHover = Array.from({ length: n }, (_, i) => ({ x: 0, y: 2 * i, z: -SHOW_ALT }));

  const forms = scenes.map((s) => buildFormation(s.shapeType, s.shapePayload, n, s.scale));
  const idx = targetIdx || forms.map(() => Array.from({ length: n }, (_, i) => i));
  const scenePos = forms.map((f, s) =>
    Array.from({ length: n }, (_, i) => f[idx[s]?.[i] ?? i] ?? f[i]),
  );

  const wps = Array.from({ length: n }, () => []);
  let t = 0;

  const push = (positions) => {
    for (let i = 0; i < n; i++) {
      const p = positions[i];
      wps[i].push({ time: round2(t), x: round2(p.x), y: round2(p.y), z: round2(p.z), yaw: 0 });
    }
  };

  const lerp = (A, B, k) => A.map((a, i) => ({
    x: a.x + (B[i].x - a.x) * k,
    y: a.y + (B[i].y - a.y) * k,
    z: a.z + (B[i].z - a.z) * k,
  }));

  // Every leg below is emitted as a HALF-OPEN interval: it pushes its starting
  // sample and every sample up to but NOT including its endpoint, then advances
  // the clock by exactly its own duration. The next leg's first sample is that
  // endpoint. One closing sample is pushed after the last leg.
  //
  // This is what makes a 6 s hold occupy exactly 6 s of show time. The previous
  // implementation emitted the endpoint too, so each leg re-sent the position
  // the previous leg had already ended on and the clock gained an extra dt per
  // leg — a 6 s hold ran 6.5 s at 2 Hz, and the error grew as the rate dropped.

  /** Holds `positions` for `secs`. */
  const hold = (positions, secs) => {
    const steps = Math.max(0, Math.round(secs * r));
    for (let k = 0; k < steps; k++) { push(positions); t += dt; }
  };

  /** Straight-line move (no transit layering) — used for vertical take-off/landing. */
  const straight = (from, to, secs) => {
    const steps = Math.max(1, Math.round(secs * r));
    for (let k = 0; k < steps; k++) { push(lerp(from, to, k / steps)); t += dt; }
  };

  /**
   * Horizontal morph with per-drone transient transit altitude.
   *
   * Crossing straight paths pass at different heights: the assignment shortens
   * travel but can't stop two segments from intersecting. Layering is centred so
   * the excursion is balanced, and the lift follows a smoothstep-rounded
   * trapezoid — full separation through the crossing-prone middle, eased corners
   * so acceleration stays in budget. z is NED (negative = up), so subtracting
   * the lift raises the drone.
   *
   * Returns the time actually consumed, which may exceed `secs` if the requested
   * pace would break the velocity budget.
   */
  const morph = (from, to, secs, gapMeters) => {
    const gap = Number.isFinite(gapMeters) && gapMeters > 0 ? gapMeters : DEFAULT_TRANSIT_GAP;
    // Auto-pace: never exceed the velocity budget regardless of the requested
    // time. Shared with computeShowDuration so the predicted and built lengths
    // can't drift apart.
    const secsSafe = pacedMorphSeconds(secs, maxTravelBetween(from, to), n, gap);
    const steps = Math.max(1, Math.round(secsSafe * r));
    const transitProfile = (kk) =>
      smooth(Math.max(0, Math.min(1, kk / TRANSIT_RAMP, (1 - kk) / TRANSIT_RAMP)));

    for (let k = 0; k < steps; k++) {
      const kk = k / steps, prof = transitProfile(kk);
      push(lerp(from, to, smooth(kk)).map((p, i) => ({
        x: p.x, y: p.y, z: p.z - gap * (i - (n - 1) / 2) * prof,
      })));
      t += dt;
    }
    return steps * dt;
  };

  straight(ground, padHover, TAKEOFF_SECONDS); // vertical take-off from pads

  for (let s = 0; s < scenePos.length; s++) {
    const from = s === 0 ? padHover : scenePos[s - 1];
    morph(from, scenePos[s], Math.max(0, scenes[s].transitionDurationSeconds || 0),
      scenes[s].transitLayerGapMeters);
    hold(scenePos[s], Math.max(0, scenes[s].holdDurationSeconds || 0));
  }

  const last = scenePos[scenePos.length - 1];
  const returnSecs = Math.max(0, scenes[scenes.length - 1].transitionDurationSeconds || 0);
  morph(last, padHover, returnSecs, scenes[scenes.length - 1].transitLayerGapMeters);
  straight(padHover, ground, LAND_SECONDS); // vertical landing

  push(ground); // closing sample — the show ends parked on the pads

  return { drones: wps.map((w, i) => ({ id: i, waypoints: w })), _duration: round2(t) };
};

/**
 * Computes the collision-free assignment chain across a scene list in ONE
 * request to /api/assign-chain, rather than one request per consecutive pair.
 *
 * The chain is inherently sequential (scene s+1's assignment depends on scene
 * s's), so a per-pair client loop could never parallelize those requests —
 * every pair paid a full round trip, one after another. Moving the loop
 * server-side collapses N-1 sequential round trips into 1, which matters as a
 * show's scene count grows.
 *
 * @returns {Promise<number[][]>} targetIdx[s][i]
 * @throws if the backend is unreachable — the caller falls back to identity.
 */
export const computeAssignmentChain = async (scenes, n, apiBase) => {
  const forms = scenes.map((s) => buildFormation(s.shapeType, s.shapePayload, n, s.scale));
  const resp = await fetch(`${apiBase}/api/assign-chain`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ forms }),
  });
  if (!resp.ok) throw new Error(`assign-chain failed (${resp.status})`);
  const data = await resp.json();
  return data.target_idx;
};

/**
 * Timeline segments for the scrubber's multi-layer track: one entry per leg
 * with its wall-clock span, so the UI can draw scene blocks against real time
 * and map a scrub position back to the scene under the playhead.
 *
 * Takes the fleet size for the same reason computeShowDuration does — a morph
 * the velocity budget slows down occupies more of the timeline than the time
 * the user typed, and the blocks have to line up with the trajectory the
 * viewport is actually playing.
 */
export const buildTimelineSegments = (scenes, n = null, rate = null) => {
  const segs = [];
  let t = 0;
  const r = rate && rate >= 1 ? rate : null;
  const quantMorph = (secs) => (r ? Math.max(1, Math.round(secs * r)) / r : secs);
  const quantLeg = (secs) => (r ? Math.max(0, Math.round(secs * r)) / r : secs);

  const add = (kind, label, secs, sceneId) => {
    if (secs <= 0) return;
    segs.push({ kind, label, sceneId, start: round2(t), end: round2(t + secs) });
    t += secs;
  };

  const paced = (secs, from, to, gap) =>
    quantMorph(from && to ? pacedMorphSeconds(secs, maxTravelBetween(from, to), n, gap) : secs);

  const useGeometry = n && n >= 1;
  const padHover = useGeometry
    ? Array.from({ length: n }, (_, i) => ({ x: 0, y: 2 * i, z: -SHOW_ALT }))
    : null;
  const scenePos = useGeometry
    ? scenes.map((s) => buildFormation(s.shapeType, s.shapePayload, n, s.scale))
    : scenes.map(() => null);

  add('takeoff', 'Takeoff', quantMorph(TAKEOFF_SECONDS), null);
  scenes.forEach((s, i) => {
    const from = i === 0 ? padHover : scenePos[i - 1];
    add('morph', i === 0 ? 'Climb-out' : `Morph → ${s.name}`,
      paced(Math.max(0, s.transitionDurationSeconds || 0), from, scenePos[i], s.transitLayerGapMeters),
      s.id);
    add('hold', s.name, quantLeg(Math.max(0, s.holdDurationSeconds || 0)), s.id);
  });
  if (scenes.length) {
    const lastScene = scenes[scenes.length - 1];
    add('morph', 'Return',
      paced(Math.max(0, lastScene.transitionDurationSeconds || 0),
        scenePos[scenePos.length - 1], padHover, lastScene.transitLayerGapMeters),
      lastScene.id);
  }
  add('land', 'Landing', quantMorph(LAND_SECONDS), null);
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
