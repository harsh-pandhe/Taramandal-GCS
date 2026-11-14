import { sceneSignature } from './sceneValidation.js';

// Ties each safety violation to the scene that causes it (issue #92).
//
// Pure functions, no React. Everything here is defensive on purpose: the input
// is a backend report plus the timeline, either of which can be partial (an old
// report, a refused show, a stub in a test), and an operator panel must never
// throw because of that.

/** Categories in the order they are listed, which is also the tie-break for equal times. */
export const CATEGORIES = ['proximity', 'velocity', 'acceleration', 'yaw_rate', 'jerk', 'altitude', 'ground'];

/** Operator wording for each classify() result. */
export const WHERE_TEXT = {
  hold: 'while holding the scene',
  transition: 'while morphing into the scene',
  return: 'during the return to the pads',
  takeoff: 'during takeoff',
  landing: 'during landing',
  unknown: 'outside the timeline',
};

const isNum = (x) => typeof x === 'number' && Number.isFinite(x);
const isSeg = (s) => !!s && typeof s === 'object' && isNum(s.start) && isNum(s.end);

/**
 * The segment with start <= t < end. At or past the last segment's end (the
 * closing sample at the show duration) the last segment is used. Null for no
 * segments, a non-numeric time, or a time before the timeline starts.
 */
export const sceneAtTime = (segments, t) => {
  if (!Array.isArray(segments) || !isNum(t)) return null;
  const segs = segments.filter(isSeg);
  if (!segs.length) return null;
  const hit = segs.find((s) => t >= s.start && t < s.end);
  if (hit) return hit;
  const last = segs[segs.length - 1];
  return t >= last.end ? last : null;
};

/**
 * 'hold' | 'transition' | 'return' | 'takeoff' | 'landing'.
 *
 * The return leg is found by position, never by its label: a morph is the
 * return when an earlier segment already belongs to the same scene (the scene's
 * own morph-in, or its hold). The last scene is the only one with a second
 * morph. Looking at earlier segments of the same scene, rather than only at
 * "after the last hold", also stays right when the last scene's hold is 0 s and
 * so has no segment at all (buildTimelineSegments drops zero-length legs).
 * Returns null when the segment is not part of `segments`.
 */
export const classify = (segment, segments) => {
  if (!segment || typeof segment !== 'object') return null;
  if (segment.kind === 'takeoff') return 'takeoff';
  if (segment.kind === 'land') return 'landing';
  if (segment.kind === 'hold') return 'hold';
  if (segment.kind !== 'morph') return null;
  const segs = Array.isArray(segments) ? segments : [];
  const at = segs.indexOf(segment);
  if (at < 0) return segment.sceneId ? 'transition' : null;
  const earlier = segs.slice(0, at).some((s) => s && s.sceneId && s.sceneId === segment.sceneId);
  return earlier ? 'return' : 'transition';
};

// ---------------------------------------------------------------- sentences --

const num = (x) => (Number.isInteger(x) ? x.toFixed(1) : String(Math.round(x * 100) / 100));
const at = (v) => (isNum(v.time) ? ` at ${num(v.time)} s` : '');
const withLimit = (v, unit) => (isNum(v.limit) ? ` (limit ${num(v.limit)} ${unit})` : '');
const droneLabel = (v) => (isNum(v.drone) ? `Drone ${v.drone}` : 'A drone');

const SENTENCES = {
  proximity: (v) => {
    const pair = Array.isArray(v.drones) ? v.drones.filter(isNum) : [];
    const who = pair.length >= 2 ? `Drones ${pair[0]} and ${pair[1]}` : 'Two drones';
    return isNum(v.distance)
      ? `${who} came ${num(v.distance)} m apart${withLimit(v, 'm')}${at(v)}`
      : `${who} came too close${at(v)}`;
  },
  velocity: (v) => (isNum(v.velocity)
    ? `${droneLabel(v)} reached ${num(v.velocity)} m/s${withLimit(v, 'm/s')}${at(v)}`
    : `${droneLabel(v)} flew too fast${at(v)}`),
  acceleration: (v) => (isNum(v.acceleration)
    ? `${droneLabel(v)} accelerated at ${num(v.acceleration)} m/s²${withLimit(v, 'm/s²')}${at(v)}`
    : `${droneLabel(v)} accelerated too hard${at(v)}`),
  yaw_rate: (v) => (isNum(v.yaw_rate)
    ? `${droneLabel(v)} turned at ${num(v.yaw_rate)} °/s${withLimit(v, '°/s')}${at(v)}`
    : `${droneLabel(v)} turned too fast${at(v)}`),
  jerk: (v) => (isNum(v.jerk)
    ? `${droneLabel(v)} changed acceleration at ${num(v.jerk)} m/s³${withLimit(v, 'm/s³')}${at(v)}`
    : `${droneLabel(v)} changed acceleration too sharply${at(v)}`),
  altitude: (v) => (isNum(v.altitude)
    ? `${droneLabel(v)} climbed to ${num(v.altitude)} m${withLimit(v, 'm')}${at(v)}`
    : `${droneLabel(v)} went above the altitude limit${at(v)}`),
  // The backend reports a below-ground altitude as a negative number.
  ground: (v) => (isNum(v.altitude)
    ? `${droneLabel(v)} went ${num(Math.abs(v.altitude))} m below ground level${at(v)}`
    : `${droneLabel(v)} went below ground level${at(v)}`),
};

/** One plain sentence for a violation of `category`; never throws. */
export const explain = (category, violation) => {
  const v = violation && typeof violation === 'object' ? violation : {};
  const make = SENTENCES[category];
  return make ? make(v) : `Safety issue (${String(category)})${at(v)}`;
};

// ----------------------------------------------------------------- grouping --

const EMPTY = (stale) => ({ stale: !!stale, groups: [], unassigned: [] });

/**
 * Groups a validation report's violations by the scene playing when they
 * happen. See the module comment: takeoff, landing and anything that cannot be
 * tied to a scene in `scenes` goes to `unassigned`.
 *
 * `stale` means the scenes changed since the report was made, so times no
 * longer line up with the timeline: no groups are produced at all.
 */
export const violationsByScene = ({ violations, segments, scenes, stale } = {}) => {
  if (stale) return EMPTY(true);
  if (!Array.isArray(segments) || !segments.some(isSeg)) return EMPTY(false);
  const sceneList = Array.isArray(scenes) ? scenes : [];
  const byId = new Map();
  sceneList.forEach((s, index) => { if (s && s.id != null) byId.set(s.id, { scene: s, index }); });

  const groups = new Map();
  const unassigned = [];
  const src = violations && typeof violations === 'object' ? violations : {};

  CATEGORIES.forEach((category, catRank) => {
    const list = src[category];
    if (!Array.isArray(list)) return;
    list.forEach((v) => {
      if (!v || typeof v !== 'object' || !isNum(v.time)) return;
      const seg = sceneAtTime(segments, v.time);
      const where = seg ? (classify(seg, segments) || 'unknown') : 'unknown';
      const item = { category, time: v.time, text: explain(category, v), where, catRank };
      const hit = seg && seg.sceneId != null ? byId.get(seg.sceneId) : null;
      if (!hit || where === 'takeoff' || where === 'landing') { unassigned.push(item); return; }
      let g = groups.get(seg.sceneId);
      if (!g) {
        g = {
          sceneId: seg.sceneId, name: hit.scene.name ?? '', index: hit.index,
          firstTime: v.time, counts: {}, items: [],
        };
        groups.set(seg.sceneId, g);
      }
      g.firstTime = Math.min(g.firstTime, v.time);
      g.counts[category] = (g.counts[category] || 0) + 1;
      g.items.push(item);
    });
  });

  const byTime = (a, b) => a.time - b.time || a.catRank - b.catRank;
  const strip = ({ catRank: _r, ...rest }) => rest;
  return {
    stale: false,
    groups: [...groups.values()]
      .sort((a, b) => a.index - b.index)
      .map((g) => ({ ...g, items: g.items.sort(byTime).map(strip) })),
    unassigned: unassigned.sort(byTime).map(strip),
  };
};

/**
 * True when a validation result no longer describes the current scenes: the
 * signature recorded when it was made differs from the scenes' content now (any
 * field of any scene, so a swapped shape with the same total length counts), or
 * none was recorded at all. Never throws.
 */
export const violationsStale = (recordedSignature, doc) => (
  typeof recordedSignature !== 'string' || recordedSignature !== sceneSignature(doc)
);
