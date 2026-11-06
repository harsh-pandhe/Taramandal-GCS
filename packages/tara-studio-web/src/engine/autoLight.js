// Auto light show: the lighting cues of a real drone show, derived from the show's own timeline.
//
//   * Lights are OFF while the drones fly (takeoff, climb-out, morphs, return, landing): time that no
//     keyframe covers is dark, so nothing is emitted for it.
//   * The moment a formation is reached (the start of each scene's hold) the lights come ON as a sweep
//     across the formation, left to right or right to left: each drone blinks its colour as the front
//     reaches it, then stays lit with a palette spread along the sweep (SWEEP_ON).
//   * For the rest of the hold the colours keep running across the formation (COLOR_CHASE).
//
// The result is an ordinary LED track (see ledPatterns.js), so it is editable, previewed live and baked
// into the export exactly like a hand-made one.

import { makeLedKeyframe } from './ledPatterns.js';

/** One palette per scene, cycled. Warm festival colours first. */
export const AUTO_LIGHT_PALETTES = [
  ['#ffb300', '#ff5722', '#ff1744'],
  ['#00e5ff', '#2979ff', '#d500f9'],
  ['#76ff03', '#ffee58', '#ff9100'],
  ['#ff4081', '#7c4dff', '#18ffff'],
];

export const AUTO_LIGHT_DIRECTIONS = [
  { value: 'ltr', label: 'Left to right' },
  { value: 'rtl', label: 'Right to left' },
  { value: 'alternate', label: 'Alternate each scene' },
];

/** Index of the first waypoint at or after time `t` (waypoints are time-ordered). */
const indexAtOrAfter = (wps, t) => {
  let lo = 0, hi = wps.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (wps[mid].time < t - 1e-6) lo = mid + 1; else hi = mid;
  }
  return lo;
};

/** Smallest and largest x of the whole fleet at show time `t`. */
const extentAt = (choreoData, t) => {
  let lo = Infinity, hi = -Infinity;
  for (const d of choreoData.drones) {
    const wp = d.waypoints[indexAtOrAfter(d.waypoints, t)];
    if (!wp) continue;
    if (wp.x < lo) lo = wp.x;
    if (wp.x > hi) hi = wp.x;
  }
  return Number.isFinite(lo) ? { lo, hi } : null;
};

const round2 = (v) => +v.toFixed(2);

/**
 * @param {object} choreoData  the built show (drones[].waypoints with time, x, y, z)
 * @param {Array}  segments    buildTimelineSegments(): [{kind, label, start, end}]
 * @param {object} [opts]
 * @param {'ltr'|'rtl'|'alternate'} [opts.direction='ltr']
 * @param {number} [opts.sweepSeconds=2.5]  time for the front to cross the formation
 * @param {number} [opts.blinkCount=3]      blinks each drone makes as the front reaches it
 * @param {number} [opts.blinkHz=4]
 * @returns {Array} LED keyframes, in time order
 */
export const buildAutoLightTrack = (choreoData, segments, opts = {}) => {
  if (!choreoData?.drones?.length || !segments?.length) return [];
  const {
    direction = 'ltr', sweepSeconds = 2.5, blinkCount = 3, blinkHz = 4,
  } = opts;

  const track = [];
  let sceneNo = 0;
  for (const seg of segments) {
    if (seg.kind !== 'hold' || seg.end - seg.start <= 0.2) continue;
    const ext = extentAt(choreoData, seg.start);
    if (!ext) continue;
    const span = Math.max(1, ext.hi - ext.lo);
    const reverse = direction === 'rtl' || (direction === 'alternate' && sceneNo % 2 === 1);
    const palette = AUTO_LIGHT_PALETTES[sceneNo % AUTO_LIGHT_PALETTES.length];
    const hold = seg.end - seg.start;

    // The sweep, with its blinks, then a second or so of steady colour before the chase takes over.
    const sweepCue = Math.min(hold, sweepSeconds + blinkCount / blinkHz + 1);
    track.push(makeLedKeyframe({
      pattern: 'SWEEP_ON', start_time: round2(seg.start), duration: round2(sweepCue),
      color: palette[0], palette, axis: 'x', reverse,
      axis_origin: round2(reverse ? ext.hi : ext.lo),
      wave_speed: round2(span / sweepSeconds), wave_length: round2(span),
      frequency_hz: blinkHz, duty_cycle: 0.5, blink_count: blinkCount,
    }));

    if (hold - sweepCue > 0.2) {
      track.push(makeLedKeyframe({
        pattern: 'COLOR_CHASE', start_time: round2(seg.start + sweepCue), duration: round2(hold - sweepCue),
        color: palette[0], palette, axis: 'x',
        wave_speed: round2((reverse ? -1 : 1) * span / 3), wave_length: round2(Math.max(8, span * 0.7)),
      }));
    }
    sceneNo += 1;
  }
  return track;
};
