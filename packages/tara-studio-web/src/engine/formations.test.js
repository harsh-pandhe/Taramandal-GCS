import { describe, it, expect } from 'vitest';
import { buildFormation, SHAPE_LIBRARY, FORMATION_LABELS, SHOW_ALT } from './formations.js';

const SHAPES = Object.keys(FORMATION_LABELS);

describe('buildFormation', () => {
  it('returns exactly n points for every parametric shape and fleet size', () => {
    for (const shape of SHAPES) {
      for (const n of [1, 2, 3, 7, 25, 100]) {
        expect(buildFormation('parametric', shape, n), `${shape}/${n}`).toHaveLength(n);
      }
    }
  });

  it('returns exactly n points for text shapes', () => {
    for (const payload of ['0', '5', '42', 'TARA']) {
      for (const n of [1, 3, 20, 100]) {
        expect(buildFormation('text', payload, n), `${payload}/${n}`).toHaveLength(n);
      }
    }
  });

  it('produces finite coordinates', () => {
    for (const shape of SHAPES) {
      for (const p of buildFormation('parametric', shape, 12)) {
        expect(Number.isFinite(p.x) && Number.isFinite(p.y) && Number.isFinite(p.z)).toBe(true);
      }
    }
    for (const p of buildFormation('text', '5', 12)) {
      expect(Number.isFinite(p.x) && Number.isFinite(p.y) && Number.isFinite(p.z)).toBe(true);
    }
  });

  it('is deterministic — the assignment chain and the build must agree', () => {
    // Both computeAssignmentChain and buildSequencedShow call this independently
    // for the same scene; if it were not pure they would place drones on
    // different point clouds.
    const a = buildFormation('parametric', 'heart', 15, 1.4);
    const b = buildFormation('parametric', 'heart', 15, 1.4);
    expect(a).toEqual(b);
  });

  it('flies formations above the ground', () => {
    for (const shape of SHAPES) {
      for (const p of buildFormation('parametric', shape, 10)) {
        expect(p.z, shape).toBeLessThan(0); // NED: negative is up
      }
    }
  });

  it('scales a formation about its own extent', () => {
    const small = buildFormation('parametric', 'circle', 12, 1.0);
    const big = buildFormation('parametric', 'circle', 12, 2.0);
    const spread = (pts) => {
      const xs = pts.map((p) => p.x);
      return Math.max(...xs) - Math.min(...xs);
    };
    expect(spread(big)).toBeCloseTo(spread(small) * 2, 5);
  });

  it('renders text as an upright wall, not flat on the ground', () => {
    // A text formation lying flat is unreadable from the ground, which defeats
    // the point — the glyph must occupy a range of altitudes.
    const pts = buildFormation('text', '5', 30);
    const zs = pts.map((p) => p.z);
    expect(Math.max(...zs) - Math.min(...zs)).toBeGreaterThan(1.0);
    // ...and stand in a single vertical plane.
    expect(new Set(pts.map((p) => p.x.toFixed(3))).size).toBe(1);
  });

  it('distinguishes different digits', () => {
    const five = buildFormation('text', '5', 30);
    const four = buildFormation('text', '4', 30);
    expect(five).not.toEqual(four);
  });

  it('falls back to a ring for unimplemented and unknown shape types', () => {
    // 'svg' and 'mesh' are in the schema but unimplemented; a scene using one
    // must still build rather than throw mid-show.
    const ring = buildFormation('parametric', 'circle', 8);
    expect(buildFormation('svg', 'whatever', 8)).toEqual(ring);
    expect(buildFormation('mesh', 'whatever', 8)).toEqual(ring);
    expect(buildFormation('parametric', 'no-such-shape', 8)).toEqual(ring);
  });

  it('handles degenerate input without throwing', () => {
    expect(buildFormation('parametric', 'circle', 0)).toEqual([]);
    expect(buildFormation('parametric', 'circle', -3)).toEqual([]);
    // A non-positive scale falls back to 1.0 rather than collapsing the fleet
    // to a single point.
    expect(buildFormation('parametric', 'circle', 6, 0))
      .toEqual(buildFormation('parametric', 'circle', 6, 1));
    expect(buildFormation('parametric', 'circle', 6, NaN))
      .toEqual(buildFormation('parametric', 'circle', 6, 1));
  });

  it('keeps formations at show altitude by default', () => {
    for (const p of buildFormation('parametric', 'circle', 10)) {
      expect(p.z).toBeCloseTo(-SHOW_ALT, 6);
    }
  });
});

describe('SHAPE_LIBRARY', () => {
  it('lists every parametric shape plus the ten digits', () => {
    expect(SHAPE_LIBRARY.filter((e) => e.shapeType === 'parametric')).toHaveLength(SHAPES.length);
    expect(SHAPE_LIBRARY.filter((e) => e.shapeType === 'text')).toHaveLength(10);
  });

  it('every entry builds a usable formation', () => {
    for (const entry of SHAPE_LIBRARY) {
      const pts = buildFormation(entry.shapeType, entry.shapePayload, 9);
      expect(pts, entry.label).toHaveLength(9);
    }
  });

  it('has a label for every entry', () => {
    for (const entry of SHAPE_LIBRARY) {
      expect(entry.label, JSON.stringify(entry)).toBeTruthy();
    }
  });
});
