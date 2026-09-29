// Formation library — each generator maps a fleet size to N target positions
// in the fleet frame, at show altitude (NED: z negative = up).
//
// A formation is a *point cloud*, not a path: the sequencer is what moves the
// fleet between them. Generators must be pure and deterministic — the same
// (shape, n, scale) always yields the same points — because the assignment
// chain and the trajectory build both call them independently and have to
// agree on the result.

import { SHOW_ALT, fleetCenter } from './layout.js';

export { SHOW_ALT };

// Minimum distance between any two points of a placed formation. The
// sequencer's straight-line morphs are only guaranteed to keep drones
// design_separation_m (2.0 m) apart when every start and goal point is at least
// sqrt(2) times that apart; 3.2 m clears 2.83 m with margin for waypoint
// rounding. Generators are free to produce tighter clouds — buildFormation
// scales them up to this floor.
export const FORMATION_SPACING = 3.2;

// Lowest altitude any formation point may sit at once scaled.
const MIN_FORMATION_ALT = 4.0;

// A glyph taller than this gets extra depth layers instead of growing further.
const TEXT_MAX_HEIGHT = 40.0;

// Horizontal reach, from its own centre, that the spacing floor may grow a flat
// formation to before it is split into stacked copies instead. Centred over a
// 100-drone pad grid (~15 m from the fleet origin), this keeps the formation
// inside the GCS's 30 m geofence.
const MAX_FORMATION_RADIUS = 14.0;

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

// ---------------------------------------------------------------- parametric

const fmtCircle = (n, scale) => {
  const r = Math.max(4, n * 0.6) * scale, cy = n;
  return Array.from({ length: n }, (_, i) => {
    const a = (2 * Math.PI * i) / n;
    return { x: 5 + r * Math.cos(a), y: cy + r * Math.sin(a), z: -SHOW_ALT };
  });
};

const fmtGrid = (n, scale) => {
  const side = Math.ceil(Math.sqrt(n)), sp = 2.5 * scale;
  return Array.from({ length: n }, (_, i) => ({
    x: (i % side) * sp,
    y: Math.floor(i / side) * sp,
    z: -SHOW_ALT,
  }));
};

const fmtLine = (n, scale) => {
  const sp = 2.5 * scale;
  return Array.from({ length: n }, (_, i) => ({ x: i * sp, y: n, z: -SHOW_ALT }));
};

const heartXY = (t) => [
  16 * Math.pow(Math.sin(t), 3),
  13 * Math.cos(t) - 5 * Math.cos(2 * t) - 2 * Math.cos(3 * t) - Math.cos(4 * t),
];

// Cumulative arc length of the unit heart over t in [0, 2π], sampled densely.
const HEART_TABLE = (() => {
  const M = 4096, ts = [0], lens = [0];
  let [px, py] = heartXY(0), total = 0;
  for (let k = 1; k <= M; k++) {
    const t = (2 * Math.PI * k) / M, [x, y] = heartXY(t);
    total += Math.hypot(x - px, y - py);
    ts.push(t); lens.push(total);
    px = x; py = y;
  }
  return { ts, lens, total };
})();

// Points spaced evenly along the outline, starting on the top notch. Equal
// steps in t crowd drones together at the notch and the tip, where the curve's
// speed drops to zero — at 50 drones two of them sat under 0.15 m apart.
const fmtHeart = (n, scale) => {
  const { ts, lens, total } = HEART_TABLE;
  let k = 0;
  return Array.from({ length: n }, (_, i) => {
    const target = (i / n) * total;
    while (k < lens.length - 2 && lens[k + 1] < target) k++;
    const span = lens[k + 1] - lens[k];
    const t = ts[k] + (span > 0 ? (target - lens[k]) / span : 0) * (ts[k + 1] - ts[k]);
    const [hx, hy] = heartXY(t);
    return { x: 5 + hx * 0.4 * scale, y: n + hy * 0.4 * scale, z: -SHOW_ALT };
  });
};

const fmtSphere = (n, scale) => {
  const r = Math.max(4, n * 0.5) * scale, cy = n, out = [];
  for (let i = 0; i < n; i++) {
    // Fibonacci sphere: even coverage without the pole clustering a naive
    // lat/lon grid produces.
    const phi = Math.acos(1 - (2 * (i + 0.5)) / n);
    const theta = Math.PI * (1 + Math.sqrt(5)) * i;
    out.push({
      x: 5 + r * Math.sin(phi) * Math.cos(theta),
      y: cy + r * Math.sin(phi) * Math.sin(theta),
      z: -SHOW_ALT - r * Math.cos(phi),
    });
  }
  return out;
};

const fmtVee = (n, scale) => {
  const sp = 2.0 * scale;
  return Array.from({ length: n }, (_, i) => {
    const side = i % 2 === 0 ? 1 : -1, k = Math.ceil(i / 2);
    return { x: k * sp, y: n + side * k * sp, z: -SHOW_ALT };
  });
};

// --------------------------------------------------------------------- text
//
// Digits/letters are drawn as seven-segment glyphs and then resampled to
// exactly N drones. Seven-segment (rather than a font outline) keeps this
// dependency-free and legible at the drone counts a real show flies — a
// vector font's fine detail is wasted when the whole glyph is 20 points.

// Segment endpoints in a 2x4 glyph box, origin bottom-left.
const SEGMENTS = {
  a: [[0, 4], [2, 4]], // top
  b: [[2, 4], [2, 2]], // upper right
  c: [[2, 2], [2, 0]], // lower right
  d: [[0, 0], [2, 0]], // bottom
  e: [[0, 2], [0, 0]], // lower left
  f: [[0, 4], [0, 2]], // upper left
  g: [[0, 2], [2, 2]], // middle
};

const GLYPHS = {
  '0': 'abcdef', '1': 'bc', '2': 'abged', '3': 'abgcd', '4': 'fgbc',
  '5': 'afgcd', '6': 'afgedc', '7': 'abc', '8': 'abcdefg', '9': 'abfgcd',
  A: 'abcefg', B: 'cdefg', C: 'adef', D: 'bcdeg', E: 'adefg', F: 'aefg',
  H: 'bcefg', L: 'def', P: 'abefg', U: 'bcdef', ' ': '',
};

// Walks a glyph's segments as one polyline-per-segment set and samples `count`
// points spread by arc length, so drones distribute evenly over the strokes
// rather than bunching at segment joins.
const sampleGlyph = (chars, count) => {
  const strokes = [];
  const GLYPH_W = 3.0; // 2 wide + 1 gap, in glyph units
  chars.split('').forEach((ch, ci) => {
    const key = GLYPHS[ch.toUpperCase()] !== undefined ? ch.toUpperCase() : '8';
    GLYPHS[key].split('').forEach((sg) => {
      const [[x1, y1], [x2, y2]] = SEGMENTS[sg];
      strokes.push({
        x1: x1 + ci * GLYPH_W, y1,
        x2: x2 + ci * GLYPH_W, y2,
        len: Math.hypot(x2 - x1, y2 - y1),
      });
    });
  });
  if (!strokes.length) return Array.from({ length: count }, () => ({ x: 0, y: 0 }));

  // Points go to strokes in proportion to length (largest remainder), and sit at
  // the centres of equal cells along their own stroke, so every point is at
  // least half a cell from its stroke's ends. A single arc-length walk across
  // all strokes could leave points from two strokes a hair either side of a
  // shared joint — nearly on top of each other.
  const total = strokes.reduce((s, k) => s + k.len, 0);
  const exact = strokes.map((k) => (count * k.len) / total);
  const alloc = exact.map(Math.floor);
  const left = count - alloc.reduce((a, b) => a + b, 0);
  strokes.map((_, i) => i)
    .sort((a, b) => (exact[b] - alloc[b]) - (exact[a] - alloc[a]) || a - b)
    .slice(0, left)
    .forEach((i) => { alloc[i] += 1; });
  const pts = [];
  strokes.forEach((k, s) => {
    for (let j = 0; j < alloc[s]; j++) {
      const u = (j + 0.5) / alloc[s];
      pts.push({ x: k.x1 + (k.x2 - k.x1) * u, y: k.y1 + (k.y2 - k.y1) * u });
    }
  });
  return pts;
};

const layerCounts = (n, d) =>
  Array.from({ length: d }, (_, k) => Math.floor(n / d) + (k < n % d ? 1 : 0));

// Renders text as a vertical wall facing the audience: the glyph's Y axis maps
// to altitude (so the text reads upright from the ground) and its X axis to the
// fleet's Y axis. A flat-on-the-ground text formation is unreadable from a
// spectator's viewpoint, which is the whole point of a text scene.
//
// The glyph unit is sized so neighbouring drones on a stroke are
// FORMATION_SPACING apart. Strokes are thin, so a big fleet on one wall makes a
// huge glyph (100 drones on a single "5" would stand ~180 m tall); past
// TEXT_MAX_HEIGHT the fleet is split into parallel copies of the wall one
// FORMATION_SPACING behind another, which reads as the same glyph from the front.
const fmtText = (n, scale, payload) => {
  const chars = (payload || '0').slice(0, 8);
  const width = (chars.length * 3 - 1);   // glyph units
  let layers = [];
  let unit = 2.2 * scale;                 // metres per glyph unit
  for (let d = 1; d <= n; d++) {
    layers = layerCounts(n, d).map((m) => sampleGlyph(chars, m));
    const tightest = Math.min(...layers.map(minPairDist));
    unit = Math.max(2.2 * scale, tightest > 0 && Number.isFinite(tightest) ? FORMATION_SPACING / tightest : 0);
    if (4 * unit <= TEXT_MAX_HEIGHT) break;
  }
  const depth = layers.length;
  return layers.flatMap((raw, d) => raw.map((p) => ({
    x: (d - (depth - 1) / 2) * FORMATION_SPACING,
    y: (p.x - width / 2) * unit,
    z: -SHOW_ALT - (p.y - 2) * unit,
  })));
};

/** Scales a point set up about its centroid until no pair is closer than FORMATION_SPACING. */
const toSpacingFloor = (pts) => {
  const m = minPairDist(pts);
  if (m >= FORMATION_SPACING) return pts;
  const k = FORMATION_SPACING / m;
  const c = pts.reduce((a, p) => ({ x: a.x + p.x / pts.length, y: a.y + p.y / pts.length, z: a.z + p.z / pts.length }), { x: 0, y: 0, z: 0 });
  return pts.map((p) => ({ x: c.x + (p.x - c.x) * k, y: c.y + (p.y - c.y) * k, z: c.z + (p.z - c.z) * k }));
};

const bboxCentre = (pts) => {
  const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
  return { x: (Math.min(...xs) + Math.max(...xs)) / 2, y: (Math.min(...ys) + Math.max(...ys)) / 2 };
};

const horizontalReach = (pts) => {
  const c = bboxCentre(pts);
  return Math.max(...pts.map((p) => Math.hypot(p.x - c.x, p.y - c.y)));
};

// A flat shape the spacing floor would grow past MAX_FORMATION_RADIUS is split
// into copies stacked FORMATION_SPACING apart in altitude — the same trade text
// makes with depth layers. Only growth the floor forces triggers this; a shape
// the user scaled up themselves keeps the size they asked for.
const stackIfOversized = (gen, n, scale) => {
  const raw = gen(n, scale);
  if (n < 2 || minPairDist(raw) >= FORMATION_SPACING
      || horizontalReach(toSpacingFloor(raw)) <= MAX_FORMATION_RADIUS) return raw;
  let stacked = raw;
  for (let d = 2; d <= n; d++) {
    const layers = layerCounts(n, d).map((m) => {
      const pts = toSpacingFloor(gen(m, scale)), c = bboxCentre(pts);
      return pts.map((p) => ({ x: p.x - c.x, y: p.y - c.y, z: p.z }));
    });
    stacked = layers.flatMap((pts, k) => pts.map((p) => ({ ...p, z: p.z - k * FORMATION_SPACING })));
    if (Math.max(...layers.map(horizontalReach)) <= MAX_FORMATION_RADIUS) break;
  }
  return stacked;
};

// Every formation goes through this: scale up to the spacing floor, centre over
// the fleet's pad grid (drones then travel the shortest distance to it and stay
// near the fleet origin the GCS geofence is measured from), and lift anything a
// scale-up pushed too close to the ground.
const place = (pts, n) => {
  const out = toSpacingFloor(pts);
  const c = bboxCentre(out), centre = fleetCenter(n);
  const dx = centre.x - c.x, dy = centre.y - c.y;
  const lowest = Math.min(...out.map((p) => -p.z));
  const dz = lowest < MIN_FORMATION_ALT ? MIN_FORMATION_ALT - lowest : 0;
  return out.map((p) => ({ x: p.x + dx, y: p.y + dy, z: p.z - dz }));
};

// ------------------------------------------------------------------ registry

const PARAMETRIC = {
  circle: fmtCircle,
  grid: fmtGrid,
  line: fmtLine,
  heart: fmtHeart,
  sphere: fmtSphere,
  vee: fmtVee,
};

export const FORMATION_LABELS = {
  circle: 'Ring',
  grid: 'Grid',
  line: 'Line',
  heart: 'Heart',
  sphere: 'Sphere',
  vee: 'V-Formation',
};

// Shapes offered in the library modal, grouped for the picker UI.
export const SHAPE_LIBRARY = [
  ...Object.keys(PARAMETRIC).map((k) => ({
    shapeType: 'parametric', shapePayload: k, label: FORMATION_LABELS[k],
  })),
  ...['0', '1', '2', '3', '4', '5', '6', '7', '8', '9'].map((d) => ({
    shapeType: 'text', shapePayload: d, label: `Digit ${d}`,
  })),
];

/**
 * Builds a formation's point cloud.
 *
 * @param {'parametric'|'text'|'svg'|'mesh'} shapeType
 * @param {string} shapePayload  e.g. "circle" for parametric, "5" for text
 * @param {number} n             fleet size
 * @param {number} scale         size multiplier (1.0 = design default)
 * @returns {{x:number,y:number,z:number}[]} exactly `n` points
 */
export const buildFormation = (shapeType, shapePayload, n, scale = 1.0) => {
  if (n < 1) return [];
  const s = Number.isFinite(scale) && scale > 0 ? scale : 1.0;
  // 'svg' and 'mesh' are declared in the scene schema but not yet implemented;
  // they fall back to a ring so a scene using them still builds and previews
  // instead of throwing mid-show-build.
  const gen = PARAMETRIC[shapePayload] || fmtCircle;
  let raw;
  if (shapeType === 'text') raw = fmtText(n, s, shapePayload);
  // A sphere isn't flat, so stacked copies would interleave; it keeps its size.
  else if (gen === fmtSphere) raw = gen(n, s);
  else raw = stackIfOversized(gen, n, s);
  // Coincident points (e.g. text that is all spaces) can't be scaled apart.
  if (n > 1 && !(minPairDist(raw) > 1e-6)) raw = fmtCircle(n, s);
  return place(raw, n);
};
