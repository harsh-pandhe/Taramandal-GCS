// Formation library — each generator maps a fleet size to N target positions
// in the fleet frame, at show altitude (NED: z negative = up).
//
// A formation is a *point cloud*, not a path: the sequencer is what moves the
// fleet between them. Generators must be pure and deterministic — the same
// (shape, n, scale) always yields the same points — because the assignment
// chain and the trajectory build both call them independently and have to
// agree on the result.

import { SHOW_ALT, fleetCenter } from './layout.js';
import { ShowRefusalError } from './showErrors.js';

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

// Studio's own site-size constraint: every generated formation must stay within
// 30m of the fleet origin.
export const SITE_RADIUS_M = 30.0;

// Mirrors tara_core.safety.DEFAULT_LIMITS.design_separation_m.
export const MIN_SEPARATION_M = 2.0;

export const MAX_ALTITUDE_M = 120.0;

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

const fmtCircle = (n, scale = 1.0) => {
  const r = Math.max(4, n * 0.6) * scale;
  return Array.from({ length: n }, (_, i) => {
    const a = (2 * Math.PI * i) / n;
    return { x: r * Math.cos(a), y: r * Math.sin(a), z: -SHOW_ALT };
  });
};

const fmtGrid = (n, scale = 1.0) => {
  const side = Math.ceil(Math.sqrt(n)), sp = 2.5 * scale;
  return Array.from({ length: n }, (_, i) => ({
    x: (i % side) * sp,
    y: Math.floor(i / side) * sp,
    z: -SHOW_ALT,
  }));
};

const fmtLine = (n, scale = 1.0) => {
  const sp = 2.5 * scale;
  return Array.from({ length: n }, (_, i) => ({ x: i * sp, y: 0, z: -SHOW_ALT }));
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
const fmtHeart = (n, scale = 1.0) => {
  const { ts, lens, total } = HEART_TABLE;
  let k = 0;
  return Array.from({ length: n }, (_, i) => {
    const target = (i / n) * total;
    while (k < lens.length - 2 && lens[k + 1] < target) k++;
    const span = lens[k + 1] - lens[k];
    const t = ts[k] + (span > 0 ? (target - lens[k]) / span : 0) * (ts[k + 1] - ts[k]);
    const [hx, hy] = heartXY(t);
    return { x: hx * 0.4 * scale, y: hy * 0.4 * scale, z: -SHOW_ALT };
  });
};

const sphereRawPoints = (count) => {
  const out = [];
  for (let i = 0; i < count; i++) {
    const phi = Math.acos(1 - (2 * (i + 0.5)) / count);
    const theta = Math.PI * (1 + Math.sqrt(5)) * i;
    out.push({
      x: Math.sin(phi) * Math.cos(theta),
      y: Math.sin(phi) * Math.sin(theta),
      z: Math.cos(phi),
    });
  }
  return out;
};

const fmtSphere = (n, scale = 1.0) => {
  const raw = sphereRawPoints(n);
  const u = minPairDist(raw);
  const rMin = n > 1 && Number.isFinite(u) && u > 0 ? FORMATION_SPACING / u : 4;
  const r = Math.min(MAX_FORMATION_RADIUS, Math.max(4, rMin)) * scale;
  return raw.map((p) => ({
    x: p.x * r,
    y: p.y * r,
    z: -SHOW_ALT - r - p.z * r,
  }));
};

const fmtVee = (n, scale = 1.0) => {
  const sp = 2.0 * scale;
  return Array.from({ length: n }, (_, i) => {
    const side = i % 2 === 0 ? 1 : -1, k = Math.ceil(i / 2);
    return { x: k * sp, y: side * k * sp, z: -SHOW_ALT };
  });
};

// --------------------------------------------------------------------- text

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

const fmtText = (n, scale, payload) => {
  const chars = (payload || '0').slice(0, 8);
  const width = (chars.length * 3 - 1); // glyph units
  let layers = [];
  let unit = 2.2 * scale; // metres per glyph unit
  for (let d = 1; d <= n; d++) {
    layers = layerCounts(n, d).map((m) => sampleGlyph(chars, m));
    const tightest = Math.min(...layers.map(minPairDist));
    unit = Math.max(2.2 * scale, tightest > 0 && Number.isFinite(tightest) ? FORMATION_SPACING / tightest : 0);
    const height = 4 * unit;
    const w = width * unit;
    if (height <= TEXT_MAX_HEIGHT && (n <= 60 || w <= 35.0)) break;
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

const maxRadiusFor = (n) => {
  const c = fleetCenter(n);
  const dist = Math.hypot(c.x, c.y);
  return Math.min(18.0, Math.max(14.0, SITE_RADIUS_M - dist - 0.2));
};

// A flat shape the spacing floor would grow past its site limit is split
// into copies stacked FORMATION_SPACING apart in altitude.
const stackIfOversized = (gen, n, scale) => {
  const raw = gen(n, scale);
  const floorPts = toSpacingFloor(raw);
  const reach = horizontalReach(floorPts);
  const maxRadius = maxRadiusFor(n);
  if (n < 2) return raw;
  if (minPairDist(raw) >= FORMATION_SPACING && reach <= maxRadius) return raw;
  if (reach <= maxRadius) return floorPts;

  let stacked = raw;
  for (let d = 2; d <= n; d++) {
    const layers = layerCounts(n, d).map((m) => {
      const pts = toSpacingFloor(gen(m, scale)), c = bboxCentre(pts);
      return pts.map((p) => ({ x: p.x - c.x, y: p.y - c.y, z: p.z }));
    });
    stacked = layers.flatMap((pts, k) => pts.map((p) => ({ ...p, z: p.z - k * FORMATION_SPACING })));
    if (Math.max(...layers.map(horizontalReach)) <= 14.0) break;
  }
  return stacked;
};

// Places the formation: scales up to spacing floor, centres over the pad grid,
// and lifts anything pushed too close to the ground.
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

export const SHAPE_LIBRARY = [
  ...Object.keys(PARAMETRIC).map((k) => ({
    shapeType: 'parametric', shapePayload: k, label: FORMATION_LABELS[k],
  })),
  ...['0', '1', '2', '3', '4', '5', '6', '7', '8', '9'].map((d) => ({
    shapeType: 'text', shapePayload: d, label: `Digit ${d}`,
  })),
];

// The raw, spacing-floor-enforced point cloud for one formation, before it
// is centred over the pad grid or clamped to the site radius. Factored out
// of buildFormation so buildSplitFormation (below) can combine several of
// these before running placement/clamping across the whole combined set.
const rawFormation = (shapeType, shapePayload, n, scale) => {
  const s = Number.isFinite(scale) && scale > 0 ? scale : 1.0;
  const gen = PARAMETRIC[shapePayload] || fmtCircle;
  let raw;
  if (shapeType === 'text') raw = fmtText(n, s, shapePayload);
  else if (gen === fmtSphere) raw = gen(n, s);
  else raw = stackIfOversized(gen, n, s);
  if (n > 1 && !(minPairDist(raw) > 1e-6)) raw = fmtCircle(n, s);
  return raw;
};

/**
 * Builds a formation's point cloud.
 *
 * @param {'parametric'|'text'|'svg'|'mesh'} shapeType
 * @param {string} shapePayload
 * @param {number} n fleet size
 * @param {number} scale size multiplier
 * @returns {{x:number,y:number,z:number}[]} exactly `n` points
 */
export const buildFormation = (shapeType, shapePayload, n, scale = 1.0) => {
  if (n < 1) return [];
  const raw = rawFormation(shapeType, shapePayload, n, scale);
  const placed = place(raw, n);

  // Enforce SITE_RADIUS_M: if pad-grid offset or large span pushes any point
  // beyond SITE_RADIUS_M (30m), first shift toward origin if it fits,
  // otherwise scale horizontally about its centroid without violating minimum spacing.
  let current = placed;
  const maxReach = current.reduce((m, p) => Math.max(m, Math.hypot(p.x, p.y)), 0);
  if (maxReach > SITE_RADIUS_M + 1e-6) {
    const xs = current.map((p) => p.x);
    const ys = current.map((p) => p.y);
    const maxX = Math.max(...xs), minX = Math.min(...xs);
    const maxY = Math.max(...ys), minY = Math.min(...ys);
    const spanX = maxX - minX;
    const spanY = maxY - minY;

    // If span fits within site diameter, shift toward origin without reducing spacing
    if (spanX <= 2 * (SITE_RADIUS_M - 0.5) && spanY <= 2 * (SITE_RADIUS_M - 0.5)) {
      const shiftX = maxX > 29.5 ? 29.5 - maxX : (minX < -29.5 ? -29.5 - minX : 0);
      const shiftY = maxY > 29.5 ? 29.5 - maxY : (minY < -29.5 ? -29.5 - minY : 0);
      current = current.map((p) => ({ ...p, x: p.x + shiftX, y: p.y + shiftY }));
    }

    // Recheck reach after shifting; scale about origin only if minimum separation is preserved
    const postShiftReach = current.reduce((m, p) => Math.max(m, Math.hypot(p.x, p.y)), 0);
    if (postShiftReach > SITE_RADIUS_M + 1e-6) {
      const k = (SITE_RADIUS_M - 0.5) / postShiftReach;
      const curDist = minPairDist(current);
      if (shapeType === 'parametric' && curDist * k < MIN_SEPARATION_M - 1e-6) {
        throw new ShowRefusalError(
          `Formation "${shapePayload}" (n=${n}, scale=${scale}) cannot fit within site radius (${SITE_RADIUS_M}m) without violating minimum spacing (${MIN_SEPARATION_M}m); required spacing would drop to ${(curDist * k).toFixed(2)}m`
        );
      }
      current = current.map((p) => ({
        ...p,
        x: p.x * k,
        y: p.y * k,
      }));
    }
  }

  const maxAlt = Math.max(...current.map((p) => -p.z));
  if (maxAlt > MAX_ALTITUDE_M + 1e-6) {
    throw new ShowRefusalError(
      `Formation "${shapePayload}" (n=${n}, scale=${scale}) exceeds maximum altitude ceiling: ${maxAlt.toFixed(1)}m > ${MAX_ALTITUDE_M}m`
    );
  }
  const finalMin = minPairDist(current);
  if (shapeType === 'parametric' && finalMin < MIN_SEPARATION_M - 1e-6) {
    throw new ShowRefusalError(
      `Formation "${shapePayload}" (n=${n}, scale=${scale}) violates minimum spacing: ${finalMin.toFixed(2)}m < ${MIN_SEPARATION_M}m`
    );
  }
  return current;
};

// --------------------------------------------------------------- split fleet

/**
 * Builds a combined point cloud for a figure plus one or more support
 * groups around it (500-drone plan, milestone M3).
 *
 * `parts[0]` is the figure; every `parts[1:]` is a support group. A support
 * group is translated outward from the figure (and from every earlier
 * support group) by whatever distance guarantees MIN_SEPARATION_M between
 * every pair of points from *different* groups — on top of each group's own
 * internal spacing, which buildFormation's generators already guarantee on
 * their own. The proof is a reverse-triangle-inequality argument: pushing a
 * group's centre to `envelopeReach + ownReach + MIN_SEPARATION_M` from the
 * shared origin means the closest its points can ever get to the origin is
 * `envelopeReach + MIN_SEPARATION_M`, while nothing placed so far is farther
 * than `envelopeReach` from it — so no pair of points from the two sets can
 * end up closer than MIN_SEPARATION_M, regardless of their exact positions.
 *
 * With more than one support group, each is spread at its own evenly-stepped
 * angle around the figure rather than stacked in one direction, so the
 * arrangement reads as "surrounded" once there are enough of them — this is
 * a first-pass geometry, not a literal ring wrap for every support shape. A
 * support group whose own shape is already a parametric ring (`circle`)
 * large enough to clear everything placed so far on its own radius is left
 * concentric instead of translated, so it wraps fully around in one piece;
 * a ring too small for that falls back to the generic translated placement
 * like any other shape, which is a visually different but still safe
 * outcome, not a crash.
 *
 * Every refusal below is a plain Error, not ShowRefusalError (#141) -
 * explicit dependency, not an oversight (review on #166): #141 isn't on
 * main yet, so showErrors.js isn't available to this branch. Once it
 * lands, every throw here should switch to ShowRefusalError, the same way
 * formations.js's and sequencer.js's own refusals will, so a refused split
 * reaching a render-phase useMemo/preview doesn't crash the page the way
 * #141/#153 originally found for other refusals.
 *
 * @param {{shapeType: string, shapePayload: string, scale?: number, count: number}[]} parts
 * @returns {{x:number,y:number,z:number}[]} the parts' combined point count, figure first then each support group in order
 */
// Release review on #141 found the same defect in its own direct-vs-layered
// trigger: a search that stops exactly at MIN_SEPARATION_M builds a pair at
// ~2.000m by construction, and export rounds every waypoint to the
// centimetre (round2 in sequencer.js, then again by export_tara's own
// int16-centimetre packing) - enough to erode a hairline margin below the
// limit (2*sqrt(3)*0.005 = 0.0173m bound; #141 uses 0.05m for its own
// margin, LAYER_CLEARANCE). Same fix here: search for this much clearance
// above the limit, not the limit itself.
const EXPORT_ROUNDING_MARGIN_M = 0.05;

export const buildSplitFormation = (parts) => {
  if (!Array.isArray(parts) || parts.length < 2) {
    throw new Error('buildSplitFormation needs a figure and at least one support group');
  }
  for (const p of parts) {
    if (!Number.isInteger(p.count) || p.count < 1) {
      throw new Error(`Every split-formation part needs an integer count >= 1 (got ${p.count})`);
    }
  }
  const totalN = parts.reduce((sum, p) => sum + p.count, 0);

  const [figure, ...supports] = parts;
  const figureRaw = rawFormation(figure.shapeType, figure.shapePayload, figure.count, figure.scale ?? 1.0);
  // Defensive (release review on #141/#166): if a generator ever returned
  // the wrong count, the rest of this function would silently misalign
  // every group's slice boundaries rather than fail closed.
  if (figureRaw.length !== figure.count) {
    throw new Error(`Figure "${figure.shapePayload}" returned ${figureRaw.length} points, not the requested ${figure.count}`);
  }
  const figureCentre = bboxCentre(figureRaw);
  const centredFigure = figureRaw.map((p) => ({ x: p.x - figureCentre.x, y: p.y - figureCentre.y, z: p.z }));

  const groups = [centredFigure];
  let envelopeReach = horizontalReach(centredFigure);

  supports.forEach((support, idx) => {
    const raw = rawFormation(support.shapeType, support.shapePayload, support.count, support.scale ?? 1.0);
    if (raw.length !== support.count) {
      throw new Error(`Support "${support.shapePayload}" returned ${raw.length} points, not the requested ${support.count}`);
    }
    const centre = bboxCentre(raw);
    const centred = raw.map((p) => ({ x: p.x - centre.x, y: p.y - centre.y, z: p.z }));
    const ownReach = horizontalReach(centred);

    const ringClearance = Math.min(...centred.map((p) => Math.hypot(p.x, p.y))) - envelopeReach;
    let placedSupport;
    if (support.shapeType === 'parametric' && support.shapePayload === 'circle'
      && ringClearance >= MIN_SEPARATION_M + EXPORT_ROUNDING_MARGIN_M) {
      placedSupport = centred;
    } else {
      // envelopeReach + ownReach + MIN_SEPARATION_M (the docstring's
      // bounding-circle proof) is always safe, but for anything other than
      // two circles it is also far more than necessary - a grid or heart's
      // actual nearest point to the rest of the fleet is usually well
      // inside its bounding circle. Binary-search down from that provably
      // safe radius to the smallest one whose *real* point-to-point minimum
      // (checked against the real groups already placed, not their
      // bounding circles) still clears MIN_SEPARATION_M, so realistic fleet
      // sizes don't get pushed outside the site radius for no reason.
      const angle = (2 * Math.PI * idx) / supports.length;
      const cos = Math.cos(angle), sin = Math.sin(angle);
      const placedSoFar = groups.flat();
      const minDistAtRadius = (r) => {
        const dx = r * cos, dy = r * sin;
        let m = Infinity;
        for (const p of centred) {
          const px = p.x + dx, py = p.y + dy;
          for (const q of placedSoFar) {
            const d = Math.hypot(px - q.x, py - q.y, p.z - q.z);
            if (d < m) m = d;
          }
        }
        return m;
      };
      let lo = 0, hi = envelopeReach + ownReach + MIN_SEPARATION_M + EXPORT_ROUNDING_MARGIN_M;
      for (let iter = 0; iter < 40 && hi - lo > 1e-3; iter++) {
        const mid = (lo + hi) / 2;
        if (minDistAtRadius(mid) >= MIN_SEPARATION_M + EXPORT_ROUNDING_MARGIN_M) hi = mid;
        else lo = mid;
      }
      const dx = hi * cos, dy = hi * sin;
      placedSupport = centred.map((p) => ({ x: p.x + dx, y: p.y + dy, z: p.z }));
    }
    groups.push(placedSupport);
    envelopeReach = Math.max(envelopeReach, ...placedSupport.map((p) => Math.hypot(p.x, p.y)));
  });

  const combined = groups.flat();

  // Centred on its own combined footprint, not buildFormation's usual
  // fleetCenter(n) pad-grid convention: a split arrangement's natural
  // footprint (figure plus however many support groups, spread however
  // many directions) has no reason to line up with the single-shape
  // per-count heuristic the way one ordinary formation does. Bbox-centring
  // on (0,0) also keeps this step from reintroducing the exact problem the
  // placement loop above just solved - shifting toward an arbitrary
  // off-centre pad-grid target can push an already-tight, already-safe
  // arrangement's farthest point back outside the site radius.
  const centre = bboxCentre(combined);
  const lowest = Math.min(...combined.map((p) => -p.z));
  const dz = lowest < MIN_FORMATION_ALT ? MIN_FORMATION_ALT - lowest : 0;
  const current = combined.map((p) => ({
    x: p.x - centre.x,
    y: p.y - centre.y,
    z: p.z - dz,
  }));

  // Site radius: refuse rather than silently shrinking (and so undoing) a
  // carefully built clearance. No shift-toward-origin step here, unlike
  // buildFormation's own: `current` is already bbox-centred a few lines up,
  // so a per-axis shift could never actually move anything (review on
  // #166: for any span within the site diameter, bbox-centring alone
  // already puts maxX/minX at exactly +-span/2, which is already within
  // the radius whenever the span is).
  const maxReach = current.reduce((m, p) => Math.max(m, Math.hypot(p.x, p.y)), 0);
  if (maxReach > SITE_RADIUS_M + 1e-6) {
    throw new Error(
      `Split formation (figure + ${supports.length} support group(s), ${totalN} drones) cannot fit within the site radius (${SITE_RADIUS_M}m) even centred; refused rather than scaled down, which would undo the clearance already built between groups.`
    );
  }

  const maxAlt = Math.max(...current.map((p) => -p.z));
  if (maxAlt > MAX_ALTITUDE_M + 1e-6) {
    throw new Error(
      `Split formation (${totalN} drones) exceeds the maximum altitude ceiling: ${maxAlt.toFixed(1)}m > ${MAX_ALTITUDE_M}m`
    );
  }

  const finalMin = minPairDist(current);
  if (finalMin < MIN_SEPARATION_M - 1e-6) {
    throw new Error(
      `Split formation (${totalN} drones) violates minimum spacing: ${finalMin.toFixed(2)}m < ${MIN_SEPARATION_M}m`
    );
  }

  return current;
};
