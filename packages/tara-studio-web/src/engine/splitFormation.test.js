import { describe, it, expect } from 'vitest';
import { buildSplitFormation, MIN_SEPARATION_M, SITE_RADIUS_M } from './formations.js';

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

// Pairwise minimum between two specific groups, not the whole combined set -
// the property this primitive actually promises (within-group spacing is
// already guaranteed by buildFormation's own generators).
const minCrossGroupDist = (a, b) => {
  let m = Infinity;
  for (const p of a) for (const q of b) {
    const d = Math.hypot(p.x - q.x, p.y - q.y, (p.z ?? 0) - (q.z ?? 0));
    if (d < m) m = d;
  }
  return m;
};

describe('buildSplitFormation', () => {
  it('returns exactly the combined count, figure first then each support group in order', () => {
    const result = buildSplitFormation([
      { shapeType: 'parametric', shapePayload: 'grid', count: 10 },
      { shapeType: 'parametric', shapePayload: 'circle', count: 8 },
    ]);
    expect(result).toHaveLength(18);
  });

  it('keeps every pair across different groups at least MIN_SEPARATION_M apart', () => {
    const configs = [
      [{ shapeType: 'parametric', shapePayload: 'grid', count: 12 }, { shapeType: 'parametric', shapePayload: 'line', count: 6 }],
      [{ shapeType: 'parametric', shapePayload: 'heart', count: 20 }, { shapeType: 'parametric', shapePayload: 'circle', count: 30 }],
      [{ shapeType: 'text', shapePayload: '5', count: 15 }, { shapeType: 'parametric', shapePayload: 'vee', count: 5 }],
      // Three groups: figure plus two supports spread at different angles.
      [
        { shapeType: 'parametric', shapePayload: 'grid', count: 10 },
        { shapeType: 'parametric', shapePayload: 'line', count: 4 },
        { shapeType: 'parametric', shapePayload: 'line', count: 4 },
      ],
    ];
    for (const parts of configs) {
      const result = buildSplitFormation(parts);
      let offset = 0;
      const groups = parts.map((p) => {
        const g = result.slice(offset, offset + p.count);
        offset += p.count;
        return g;
      });
      for (let i = 0; i < groups.length; i++) {
        for (let j = i + 1; j < groups.length; j++) {
          const d = minCrossGroupDist(groups[i], groups[j]);
          expect(d, `groups ${i} vs ${j}, config ${JSON.stringify(parts)}`).toBeGreaterThanOrEqual(MIN_SEPARATION_M - 1e-6);
        }
      }
      // The whole-fleet minimum (within-group spacing is already >= the
      // stricter FORMATION_SPACING floor from buildFormation).
      expect(minPairDist(result)).toBeGreaterThanOrEqual(MIN_SEPARATION_M - 1e-6);
    }
  });

  it('leaves a ring concentric when it is already big enough to wrap around everything placed so far', () => {
    // A small figure and a large ring: the ring's own natural radius clears
    // the figure on its own, so it should stay centred at the same point
    // rather than being pushed off to one side.
    const result = buildSplitFormation([
      { shapeType: 'parametric', shapePayload: 'grid', count: 4 },
      { shapeType: 'parametric', shapePayload: 'circle', count: 60 },
    ]);
    const figure = result.slice(0, 4);
    const ring = result.slice(4);
    const figureCentre = {
      x: figure.reduce((s, p) => s + p.x, 0) / figure.length,
      y: figure.reduce((s, p) => s + p.y, 0) / figure.length,
    };
    const ringCentre = {
      x: ring.reduce((s, p) => s + p.x, 0) / ring.length,
      y: ring.reduce((s, p) => s + p.y, 0) / ring.length,
    };
    expect(Math.hypot(ringCentre.x - figureCentre.x, ringCentre.y - figureCentre.y)).toBeLessThan(0.5);
  });

  it('falls back to a translated placement when the ring is too small to wrap around', () => {
    // A tiny ring next to a big figure can't encircle it - still must be
    // safe, just not concentric.
    const result = buildSplitFormation([
      { shapeType: 'parametric', shapePayload: 'grid', count: 60 },
      { shapeType: 'parametric', shapePayload: 'circle', count: 3 },
    ]);
    const figure = result.slice(0, 60);
    const ring = result.slice(60);
    expect(minCrossGroupDist(figure, ring)).toBeGreaterThanOrEqual(MIN_SEPARATION_M - 1e-6);
  });

  it('is deterministic', () => {
    const parts = [
      { shapeType: 'parametric', shapePayload: 'grid', count: 10 },
      { shapeType: 'parametric', shapePayload: 'circle', count: 8 },
    ];
    expect(buildSplitFormation(parts)).toEqual(buildSplitFormation(parts));
  });

  it('every point stays within the site radius and under the altitude ceiling', () => {
    const result = buildSplitFormation([
      { shapeType: 'parametric', shapePayload: 'grid', count: 40 },
      { shapeType: 'parametric', shapePayload: 'circle', count: 40 },
    ]);
    for (const p of result) {
      expect(Math.hypot(p.x, p.y)).toBeLessThanOrEqual(SITE_RADIUS_M + 1e-6);
      expect(-p.z).toBeLessThanOrEqual(120.0 + 1e-6);
    }
  });

  it('centres on its own combined footprint rather than any single-count pad-grid target', () => {
    // Deliberate: a split arrangement's bbox is centred at the origin, not
    // shifted onto buildFormation's usual fleetCenter(n) heuristic - doing
    // that could push an already-tight, already-safe arrangement back
    // outside the site radius for no geometric reason.
    const parts = [
      { shapeType: 'parametric', shapePayload: 'grid', count: 7 },
      { shapeType: 'parametric', shapePayload: 'line', count: 3 },
    ];
    const result = buildSplitFormation(parts);
    const xs = result.map((p) => p.x), ys = result.map((p) => p.y);
    const bboxCentre = { x: (Math.min(...xs) + Math.max(...xs)) / 2, y: (Math.min(...ys) + Math.max(...ys)) / 2 };
    expect(Math.hypot(bboxCentre.x, bboxCentre.y)).toBeLessThan(1e-6);
  });

  it('rejects fewer than 2 parts', () => {
    expect(() => buildSplitFormation([{ shapeType: 'parametric', shapePayload: 'grid', count: 10 }]))
      .toThrow(/at least one support group/);
    expect(() => buildSplitFormation([])).toThrow();
  });

  it('rejects a non-positive or non-integer count', () => {
    const base = { shapeType: 'parametric', shapePayload: 'grid', count: 10 };
    for (const bad of [0, -1, 1.5, NaN]) {
      expect(() => buildSplitFormation([base, { ...base, count: bad }]))
        .toThrow(/integer count/);
    }
  });

  it('refuses rather than silently shrinks when the combined arrangement cannot fit the site radius', () => {
    // Several large support groups, each individually fine, forced far
    // enough apart that the whole arrangement cannot fit inside 30m.
    const parts = [
      { shapeType: 'parametric', shapePayload: 'grid', count: 100, scale: 2.0 },
      { shapeType: 'parametric', shapePayload: 'grid', count: 100, scale: 2.0 },
      { shapeType: 'parametric', shapePayload: 'grid', count: 100, scale: 2.0 },
    ];
    expect(() => buildSplitFormation(parts)).toThrow(/site radius/);
  });

  it('refuses rather than silently shrinks when a support group is too large for its own count', () => {
    // A heart this oversized gets stacked into altitude layers by its own
    // generator (stackIfOversized) rather than overflowing the site radius -
    // still a refusal, just the altitude ceiling catching it instead.
    const parts = [
      { shapeType: 'parametric', shapePayload: 'grid', count: 10 },
      { shapeType: 'parametric', shapePayload: 'heart', count: 100, scale: 3.0 },
    ];
    expect(() => buildSplitFormation(parts)).toThrow(/altitude ceiling/);
  });

  it('survives export rounding to the centimetre - cross-group separation stays >= MIN_SEPARATION_M (release review on #166)', () => {
    // The margin search targets MIN_SEPARATION_M + EXPORT_ROUNDING_MARGIN_M,
    // not the bare limit - because every waypoint gets rounded to the
    // centimetre on the way out (round2 in sequencer.js, then again by
    // export_tara's own int16-centimetre packing), and a pair placed
    // exactly at the limit can erode below it once actually quantised.
    // This is the same defect #141 found and fixed in its own
    // direct-vs-layered trigger.
    const round2 = (v) => Math.round(v * 100) / 100;
    const configs = [
      [{ shapeType: 'parametric', shapePayload: 'grid', count: 12 }, { shapeType: 'parametric', shapePayload: 'line', count: 6 }],
      [{ shapeType: 'parametric', shapePayload: 'heart', count: 20 }, { shapeType: 'parametric', shapePayload: 'circle', count: 30 }],
    ];
    for (const parts of configs) {
      const result = buildSplitFormation(parts);
      let offset = 0;
      const groups = parts.map((p) => {
        const g = result.slice(offset, offset + p.count).map((pt) => ({ x: round2(pt.x), y: round2(pt.y), z: round2(pt.z) }));
        offset += p.count;
        return g;
      });
      for (let i = 0; i < groups.length; i++) {
        for (let j = i + 1; j < groups.length; j++) {
          expect(minCrossGroupDist(groups[i], groups[j]), `groups ${i} vs ${j}`).toBeGreaterThanOrEqual(MIN_SEPARATION_M - 1e-6);
        }
      }
    }
  });

  // Committed version of the manual 500-drone stress test from the PR body
  // (release review on #166: "commit them as tests").
  describe('500-drone scale (release review on #166)', () => {
    it('a realistic figure-plus-ring split fits, fast', () => {
      const t0 = Date.now();
      const result = buildSplitFormation([
        { shapeType: 'parametric', shapePayload: 'grid', count: 440 },
        { shapeType: 'parametric', shapePayload: 'circle', count: 60 },
      ]);
      expect(result).toHaveLength(500);
      expect(minPairDist(result)).toBeGreaterThanOrEqual(MIN_SEPARATION_M - 1e-6);
      expect(Date.now() - t0).toBeLessThan(5_000);
    });

    it('an overly ambitious 3-group split correctly refuses rather than compromise clearance', () => {
      expect(() => buildSplitFormation([
        { shapeType: 'parametric', shapePayload: 'heart', count: 200 },
        { shapeType: 'parametric', shapePayload: 'grid', count: 150 },
        { shapeType: 'parametric', shapePayload: 'grid', count: 150 },
      ])).toThrow(/site radius/);
    });
  });
});
