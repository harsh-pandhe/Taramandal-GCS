// Formation library — each generator maps a fleet size to N target positions
// in the fleet frame, at show altitude (NED: z negative = up).
//
// A formation is a *point cloud*, not a path: the sequencer is what moves the
// fleet between them. Generators must be pure and deterministic — the same
// (shape, n, scale) always yields the same points — because the assignment
// chain and the trajectory build both call them independently and have to
// agree on the result.

export const SHOW_ALT = 8.0; // metres above ground for formations

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

const fmtHeart = (n, scale) => Array.from({ length: n }, (_, i) => {
  const t = (2 * Math.PI * i) / n;
  const hx = 16 * Math.pow(Math.sin(t), 3);
  const hy = 13 * Math.cos(t) - 5 * Math.cos(2 * t) - 2 * Math.cos(3 * t) - Math.cos(4 * t);
  return { x: 5 + hx * 0.4 * scale, y: n + hy * 0.4 * scale, z: -SHOW_ALT };
});

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

  const total = strokes.reduce((s, k) => s + k.len, 0);
  const pts = [];
  for (let i = 0; i < count; i++) {
    // Sample at stroke-midpoints of equal arc-length cells: distributes points
    // evenly and never lands two drones exactly on a shared segment endpoint.
    let target = ((i + 0.5) / count) * total;
    let s = 0;
    while (s < strokes.length - 1 && target > strokes[s].len) {
      target -= strokes[s].len;
      s++;
    }
    const k = strokes[s];
    const u = k.len > 0 ? Math.min(1, target / k.len) : 0;
    pts.push({ x: k.x1 + (k.x2 - k.x1) * u, y: k.y1 + (k.y2 - k.y1) * u });
  }
  return pts;
};

// Renders text as a vertical wall facing the audience: the glyph's Y axis maps
// to altitude (so the text reads upright from the ground) and its X axis to the
// fleet's Y axis. A flat-on-the-ground text formation is unreadable from a
// spectator's viewpoint, which is the whole point of a text scene.
const fmtText = (n, scale, payload) => {
  const chars = (payload || '0').slice(0, 8);
  const raw = sampleGlyph(chars, n);
  const UNIT = 2.2 * scale;              // metres per glyph unit
  const width = (chars.length * 3 - 1);   // glyph units
  return raw.map((p) => ({
    x: 5,
    y: n + (p.x - width / 2) * UNIT,
    z: -SHOW_ALT - (p.y - 2) * UNIT,
  }));
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
  if (shapeType === 'text') return fmtText(n, s, shapePayload);
  // 'svg' and 'mesh' are declared in the scene schema but not yet implemented;
  // they fall back to a ring so a scene using them still builds and previews
  // instead of throwing mid-show-build.
  const gen = PARAMETRIC[shapePayload] || fmtCircle;
  return gen(n, s);
};
