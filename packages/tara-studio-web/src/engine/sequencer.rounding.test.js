// Post-quantization separation of direct morphs (PR #141 review, rounding defect).
//
// Waypoints are exported rounded to 1 cm per axis, independently per drone, so a
// morph whose exact separation is 2.0001 m can export at 1.99 m. These tests
// feed hand-placed formations through the real sequencer and measure what is
// exported with a checker written independently of the sequencer's own.
import { describe, it, expect, vi } from 'vitest';

const REG = vi.hoisted(() => ({ forms: {} }));
vi.mock('./formations.js', () => ({
  buildFormation: (_type, payload) => REG.forms[payload].map((p) => ({ ...p })),
}));

import {
  buildSequencedShow, makeScene, minMorphSeparation, minExportedMorphSeparation,
  measureExportedGeometry, MorphRefusalError, buildTimelineSegments,
} from './sequencer.js';

const LIMIT = 2.0;

/** Independent exact check: min over drone pairs of the piecewise-linear exported paths. */
const exportedMin = (show) => {
  let min = Infinity;
  const d = show.drones;
  for (let i = 0; i < d.length; i++) {
    for (let j = i + 1; j < d.length; j++) {
      for (let k = 0; k + 1 < d[i].waypoints.length; k++) {
        const a0 = d[i].waypoints[k], a1 = d[i].waypoints[k + 1];
        const b0 = d[j].waypoints[k], b1 = d[j].waypoints[k + 1];
        const r0 = [a0.x - b0.x, a0.y - b0.y, a0.z - b0.z];
        const r1 = [a1.x - b1.x, a1.y - b1.y, a1.z - b1.z];
        const v = r1.map((c, q) => c - r0[q]);
        const vv = v[0] ** 2 + v[1] ** 2 + v[2] ** 2;
        const w = vv < 1e-18 ? 0 : Math.max(0, Math.min(1, -(r0[0] * v[0] + r0[1] * v[1] + r0[2] * v[2]) / vv));
        min = Math.min(min, Math.hypot(r0[0] + w * v[0], r0[1] + w * v[1], r0[2] + w * v[2]));
      }
    }
  }
  return min;
};

const show2 = (A, B, { rate = 4, secs = 5 } = {}) => {
  REG.forms = { A, B };
  const scenes = [
    makeScene({ id: 'a', name: 'A', shapePayload: 'A', transitionDurationSeconds: 5, holdDurationSeconds: 2 }),
    makeScene({ id: 'b', name: 'B', shapePayload: 'B', transitionDurationSeconds: secs, holdDurationSeconds: 2 }),
  ];
  return buildSequencedShow(scenes, A.length, rate, [[0, 1], [0, 1], [0, 1]]);
};

// Found by seeded search: exact A->B separation 2.0001 m (passes `sep >= 2.0`),
// but the lockstep pair rounds independently every sample and exports 1.9925 m.
const A = [{ x: 2.47, y: 2.44, z: -30 }, { x: 3.99, y: 3.74, z: -30 }];
const B = [{ x: 1.58, y: 7.78, z: -30 }, { x: 3.1, y: 9.08, z: -30 }];

describe('direct-morph rounding (PR #141)', () => {
  it('reproduces the defect: nominally compliant, exports below 2.0 m when flown directly', () => {
    expect(minMorphSeparation(A, B)).toBeGreaterThanOrEqual(LIMIT);
    expect(minExportedMorphSeparation(A, B, 20)).toBeLessThan(LIMIT);
    expect(minExportedMorphSeparation(A, B, 20)).toBeGreaterThan(1.99 - 0.01);
  });

  it('never exports that morph below 2.0 m: it is layered or refused', () => {
    let show;
    try { show = show2(A, B); } catch (e) { expect(e).toBeInstanceOf(MorphRefusalError); return; }
    expect(exportedMin(show)).toBeGreaterThanOrEqual(LIMIT - 1e-9);
    expect(show._minSeparationM).toBeGreaterThanOrEqual(LIMIT - 1e-9);
    const zs = show.drones[0].waypoints.map((w) => w.z);
    expect(Math.max(...zs) - Math.min(...zs)).toBeGreaterThan(2); // altitude layering was used
  });

  it('the rounding allowance covers the worst case: 2*0.005 per axis over 3 axes', () => {
    // sqrt(3) * 0.01 = 0.017320...; the layer clearance (2.05) must exceed it.
    expect(2.0 + Math.sqrt(3) * 0.01).toBeLessThan(2.05);
  });

  it.each([
    ['well above the limit', 3.0],
    ['just above the limit', 2.0001],
    ['exactly at the limit', 2.0],
    ['below the limit but on the 1 cm grid it exports to 2.00', 1.9999],
    ['just below the limit (1.994 exports as 1.99)', 1.994],
    ['clearly below the limit', 1.99],
  ])('separation %s (%d m) never exports below 2.0 m', (_name, gap) => {
    const a = [{ x: 0, y: 0, z: -30 }, { x: gap, y: 0, z: -30 }];
    const b = [{ x: 0, y: 5, z: -30 }, { x: gap, y: 5, z: -30 }];
    let show;
    try { show = show2(a, b); } catch (e) {
      expect(e).toBeInstanceOf(MorphRefusalError);
      if (Math.round(gap * 100) / 100 < LIMIT) expect(e.code).toBe('ENDPOINT_SPACING'); // diagnostic names the cause
      return;
    }
    expect(Math.round(gap * 100) / 100).toBeGreaterThanOrEqual(LIMIT); // only a formation whose exported spacing is >= 2 m builds
    expect(exportedMin(show)).toBeGreaterThanOrEqual(LIMIT - 1e-9);
  });

  it('opposite-direction quantization (x rounds up, y rounds down) is covered', () => {
    const a = [{ x: 0.004, y: 0.006, z: -30 }, { x: 2.0, y: 0.006, z: -30 }];
    const b = [{ x: 4.004, y: 3.006, z: -30 }, { x: 6.0, y: 3.006, z: -30 }];
    let show;
    try { show = show2(a, b); } catch (e) { expect(e).toBeInstanceOf(MorphRefusalError); return; }
    expect(exportedMin(show)).toBeGreaterThanOrEqual(LIMIT - 1e-9);
  });

  it('reports intermediate and final frames: the built show carries its measured minimum', () => {
    const show = show2(
      [{ x: 0, y: 0, z: -30 }, { x: 4, y: 0, z: -30 }],
      [{ x: 0, y: 6, z: -30 }, { x: 4, y: 6, z: -30 }],
    );
    const geo = measureExportedGeometry(show.drones);
    expect(show._minSeparationM).toBeCloseTo(geo.min, 9);
    expect(geo.min).toBeGreaterThanOrEqual(LIMIT - 1e-9);
    expect(geo.maxAltitude).toBeLessThanOrEqual(120);
  });

  it('fuzz: 400 seeded random pair morphs never export below 2.0 m (built or refused)', () => {
    let seed = 987654321;
    const rnd = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 2 ** 32);
    let built = 0, refused = 0;
    for (let t = 0; t < 400; t++) {
      const gap = 2.0 + rnd() * 0.03; // the band the rounding bound matters in
      const th = rnd() * 2 * Math.PI;
      const c = (v) => Math.round(v * 100) / 100;
      const a0 = { x: c(rnd() * 4), y: c(rnd() * 4), z: -30 };
      const a1 = { x: c(a0.x + gap * Math.cos(th)), y: c(a0.y + gap * Math.sin(th)), z: -30 };
      const b0 = { x: c(rnd() * 20 - 10), y: c(rnd() * 20 - 10), z: -30 };
      const b1 = { x: c(b0.x + (a1.x - a0.x)), y: c(b0.y + (a1.y - a0.y)), z: -30 };
      let show;
      try { show = show2([a0, a1], [b0, b1], { rate: 4 + (t % 3) * 3 }); } catch (e) {
        expect(e).toBeInstanceOf(MorphRefusalError);
        refused++;
        continue;
      }
      built++;
      expect(exportedMin(show), `case ${t}`).toBeGreaterThanOrEqual(LIMIT - 1e-9);
    }
    expect(built + refused).toBe(400);
    expect(built).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------------------
// Final export gate, real layering, and multi-drone coverage (PR #141 review 2).
// ---------------------------------------------------------------------------

const rt = (v) => Math.round(v * 100) / 100;
const onGrid = (v) => Math.abs(v * 100 - Math.round(v * 100)) < 1e-6;

describe('final export gate (strict, same convention as the backend)', () => {
  // Two drones exactly 2.00 m apart on the 1 cm grid, whose float distance is a
  // hair under 2.0. The planner reasons with a 1e-9 allowance for ulp noise and
  // lets this scene through; only the final gate on the exported waypoints
  // catches it. The backend validator rejects the identical coordinates
  // (packages/tara-studio/tests/test_validator_strict_separation.py).
  const NOISY = [{ x: -17.99, y: 0, z: -30 }, { x: -15.99, y: 0, z: -30 }];
  const FAR = [{ x: -17.99, y: 6, z: -30 }, { x: -15.99, y: 6, z: -30 }];

  it('the fixture really is below 2.0 in floating point yet exactly 2.00 on the grid', () => {
    const d = Math.hypot(NOISY[1].x - NOISY[0].x, 0, 0);
    expect(d).toBeLessThan(2.0);
    expect(d).toBeGreaterThan(2.0 - 1e-9);
    expect(rt(NOISY[1].x - NOISY[0].x)).toBe(2.0);
    expect(onGrid(NOISY[0].x) && onGrid(NOISY[1].x)).toBe(true);
  });

  it('buildSequencedShow throws EXPORT_SEPARATION with the offending pair and time', () => {
    let err;
    try { show2(NOISY, FAR); } catch (e) { err = e; }
    expect(err).toBeInstanceOf(MorphRefusalError);
    expect(err.code).toBe('EXPORT_SEPARATION');
    expect(err.drones).toEqual([0, 1]);
    expect(err.separation).toBeLessThan(2.0);
    expect(err.separation).toBeGreaterThan(2.0 - 1e-9);
    expect(Number.isFinite(err.time) && err.time >= 0).toBe(true);
    expect(err.message).toMatch(/drones 0 and 1/);
    expect(err.message).toMatch(/minimum separation/);
  });

  it('measureExportedGeometry applies no tolerance', () => {
    const wp = (x, t) => ({ time: t, x, y: 0, z: -30, yaw: 0 });
    const below = [
      { id: 0, waypoints: [wp(-17.99, 0), wp(-17.99, 1)] },
      { id: 1, waypoints: [wp(-15.99, 0), wp(-15.99, 1)] },
    ];
    expect(measureExportedGeometry(below).min).toBeLessThan(2.0);
    const exact = [
      { id: 0, waypoints: [wp(0, 0), wp(0, 1)] },
      { id: 1, waypoints: [wp(2, 0), wp(2, 1)] },
    ];
    expect(measureExportedGeometry(exact).min).toBe(2.0); // exactly 2.0 is compliant
  });

  it('a scene exactly 2.00 m apart in exact floating point still builds (no over-refusal)', () => {
    const show = show2(
      [{ x: 0, y: 0, z: -30 }, { x: 2, y: 0, z: -30 }],
      [{ x: 0, y: 6, z: -30 }, { x: 2, y: 6, z: -30 }],
    );
    expect(show._minSeparationM).toBeGreaterThanOrEqual(2.0);
  });
});

describe('risky morphs are layered, not merely refused', () => {
  const segments = (A_, B_) => {
    REG.forms = { A: A_, B: B_ };
    const scenes = [
      makeScene({ id: 'a', name: 'A', shapePayload: 'A', transitionDurationSeconds: 5, holdDurationSeconds: 2 }),
      makeScene({ id: 'b', name: 'B', shapePayload: 'B', transitionDurationSeconds: 5, holdDurationSeconds: 2 }),
    ];
    const target = [[0, 1], [0, 1], [0, 1]];
    return { show: buildSequencedShow(scenes, 2, 4, target), segs: buildTimelineSegments(scenes, 2, 4, target) };
  };
  const window = (show, segs, label) => {
    const seg = segs.find((s) => s.label === label);
    return show.drones.map((d) => d.waypoints.filter((w) => w.time >= seg.start && w.time <= seg.end));
  };

  it('the 2.0001 m morph that exports 1.9925 m when flown directly builds, and via altitude layers', () => {
    expect(minMorphSeparation(A, B)).toBeGreaterThanOrEqual(LIMIT); // legacy rule: fly direct
    const { show, segs } = segments(A, B);
    const win = window(show, segs, 'Morph → B');
    const zs = win.flat().map((w) => w.z);
    expect(Math.min(...zs)).toBeLessThanOrEqual(-30 - 2.05 + 1e-9); // climbed a full layer gap
    expect(exportedMin(show)).toBeGreaterThanOrEqual(LIMIT);
    expect(measureExportedGeometry(show.drones).min).toBeGreaterThanOrEqual(LIMIT);
    // and it lands on formation B, on the 1 cm grid
    const end = win.map((w) => w.at(-1));
    end.forEach((w, i) => { expect([w.x, w.y, w.z]).toEqual([B[i].x, B[i].y, B[i].z]); });
  });

  it('a comfortably separated morph is NOT layered (no blanket margin)', () => {
    const a = [{ x: 0, y: 0, z: -30 }, { x: 4, y: 0, z: -30 }];
    const b = [{ x: 0, y: 6, z: -30 }, { x: 4, y: 6, z: -30 }];
    const { show, segs } = segments(a, b);
    const zs = window(show, segs, 'Morph → B').flat().map((w) => w.z);
    expect(new Set(zs)).toEqual(new Set([-30]));
  });

  it('measured on the exported leg, the same morph flown directly is below the limit', () => {
    expect(minExportedMorphSeparation(A, B, 20)).toBeLessThan(LIMIT);
  });
});

describe('multi-drone seeded fuzz on the final exported geometry', () => {
  // Deterministic LCG; formations are random sets on the 1 cm grid with a
  // pairwise spacing of at least 2.0 m, so only rounding and path crossings can
  // threaten the limit.
  const lcg = (seed) => () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 2 ** 32);
  const formation = (rnd, n, z) => {
    const pts = [];
    while (pts.length < n) {
      const p = { x: rt(rnd() * 14 - 7), y: rt(rnd() * 14 - 7), z };
      if (pts.every((q) => Math.hypot(p.x - q.x, p.y - q.y) >= 2.0)) pts.push(p);
    }
    return pts;
  };
  const independentMin = (drones) => {
    let min = Infinity;
    for (let i = 0; i < drones.length; i++) for (let j = i + 1; j < drones.length; j++) {
      for (let k = 0; k + 1 < drones[i].waypoints.length; k++) {
        const a0 = drones[i].waypoints[k], a1 = drones[i].waypoints[k + 1];
        const b0 = drones[j].waypoints[k], b1 = drones[j].waypoints[k + 1];
        const r0 = [a0.x - b0.x, a0.y - b0.y, a0.z - b0.z];
        const v = [a1.x - b1.x - r0[0], a1.y - b1.y - r0[1], a1.z - b1.z - r0[2]];
        const vv = v[0] ** 2 + v[1] ** 2 + v[2] ** 2;
        const w = vv < 1e-18 ? 0 : Math.max(0, Math.min(1, -(r0[0] * v[0] + r0[1] * v[1] + r0[2] * v[2]) / vv));
        min = Math.min(min, Math.hypot(r0[0] + w * v[0], r0[1] + w * v[1], r0[2] + w * v[2]));
      }
    }
    return min;
  };

  it('120 seeded 4-6 drone, 3-scene shows: built shows serialise on-grid, >= 2.0 m, <= 120 m; the rest are typed refusals', () => {
    const rnd = lcg(20261020);
    let built = 0, refused = 0, layered = 0;
    for (let t = 0; t < 120; t++) {
      const n = 4 + (t % 3);
      REG.forms = { A: formation(rnd, n, -30), B: formation(rnd, n, -30), C: formation(rnd, n, -34) };
      const scenes = ['A', 'B', 'C'].map((k, i) => makeScene({
        id: `s${i}`, name: k, shapePayload: k,
        transitionDurationSeconds: 3 + Math.floor(rnd() * 5), holdDurationSeconds: 1,
      }));
      const perm = () => Array.from({ length: n }, (_, i) => i).sort(() => rnd() - 0.5);
      const target = [perm(), perm(), perm(), perm()];
      let show;
      try { show = buildSequencedShow(scenes, n, [4, 5, 10][t % 3], target); } catch (e) {
        expect(e, `case ${t}`).toBeInstanceOf(MorphRefusalError);
        refused++;
        continue;
      }
      built++;
      const wire = JSON.parse(JSON.stringify({ drones: show.drones })); // serialised form
      for (const d of wire.drones) for (const w of d.waypoints) {
        expect(onGrid(w.x) && onGrid(w.y) && onGrid(w.z) && onGrid(w.time), `case ${t} grid`).toBe(true);
      }
      expect(independentMin(wire.drones), `case ${t} separation`).toBeGreaterThanOrEqual(2.0);
      expect(measureExportedGeometry(wire.drones).min, `case ${t} gate`).toBeGreaterThanOrEqual(2.0);
      expect(measureExportedGeometry(wire.drones).maxAltitude, `case ${t} altitude`).toBeLessThanOrEqual(120);
      if (wire.drones.some((d) => d.waypoints.some((w) => w.z < -36.5))) layered++;
    }
    expect(built + refused).toBe(120);
    expect(built).toBeGreaterThan(30);
    expect(layered).toBeGreaterThan(0); // the layering path is exercised, not just trivial shows
  });
});

describe('altitude ceiling on the exported waypoints (120 m, strict)', () => {
  const at = (z) => [{ x: 0, y: 0, z }, { x: 4, y: 0, z }];
  it('a formation above 120 m is refused with EXPORT_CEILING and the measured altitude', () => {
    let err;
    try { show2(at(-120.01), at(-120.01)); } catch (e) { err = e; }
    expect(err).toBeInstanceOf(MorphRefusalError);
    expect(err.code).toBe('EXPORT_CEILING');
    expect(err.altitude).toBeCloseTo(120.01, 9);
  });
  it('exactly 120.00 m is compliant', () => {
    const show = show2(at(-120), at(-120));
    expect(measureExportedGeometry(show.drones).maxAltitude).toBe(120);
  });
});
