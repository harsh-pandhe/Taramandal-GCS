import { describe, it, expect } from 'vitest';
import {
  buildFormation, SHAPE_LIBRARY, FORMATION_LABELS, SHOW_ALT, FORMATION_SPACING, MIN_SEPARATION_M,
} from './formations.js';
import { fleetCenter } from './layout.js';
import { makeScene, buildSequencedShow } from './sequencer.js';

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
    const pts = buildFormation('text', '5', 12);
    const zs = pts.map((p) => p.z);
    expect(Math.max(...zs) - Math.min(...zs)).toBeGreaterThan(1.0);
    // ...and stand in a single vertical plane.
    expect(new Set(pts.map((p) => p.x.toFixed(3))).size).toBe(1);
  });

  it('gives a large fleet depth layers rather than an ever-taller glyph', () => {
    // 100 drones on one seven-segment "5" at safe spacing would stand ~180 m
    // tall; the fleet is split into parallel copies of the wall instead.
    const pts = buildFormation('text', '5', 100);
    const alts = pts.map((p) => -p.z);
    expect(Math.max(...alts) - Math.min(...alts)).toBeLessThanOrEqual(40 + 1e-6);
    const planes = [...new Set(pts.map((p) => p.x.toFixed(3)))].map(Number).sort((a, b) => a - b);
    expect(planes.length).toBeGreaterThan(1);
    for (let i = 1; i < planes.length; i++) expect(planes[i] - planes[i - 1]).toBeCloseTo(FORMATION_SPACING, 6);
  });

  it('distinguishes different digits', () => {
    const five = buildFormation('text', '5', 30);
    const four = buildFormation('text', '4', 30);
    expect(five).not.toEqual(four);
  });

  it('falls back to a ring for unimplemented and unknown shape types', () => {
    // 'mesh' is in the schema but unimplemented; unknown parametric shapes also fall back.
    const ring = buildFormation('parametric', 'circle', 8);
    expect(buildFormation('mesh', 'whatever', 8)).toEqual(ring);
    expect(buildFormation('parametric', 'no-such-shape', 8)).toEqual(ring);
  });

  it('rejects invalid, malformed, or unsafe SVG without silent fallback', () => {
    expect(() => buildFormation('svg', 'whatever', 8)).toThrow();
    expect(() => buildFormation('svg', '<svg></svg>', 8)).toThrow();
    expect(() => buildFormation('svg', '<svg><path d="M 0 0 A 10 10 0 0 1 20 20" /></svg>', 8)).toThrow();
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

describe('formation placement', () => {
  const minPair = (pts) => {
    let m = Infinity;
    for (let i = 0; i < pts.length; i++) {
      for (let j = i + 1; j < pts.length; j++) {
        m = Math.min(m, Math.hypot(pts[i].x - pts[j].x, pts[i].y - pts[j].y, pts[i].z - pts[j].z));
      }
    }
    return m;
  };
  const all = [
    ...SHAPES.map((shape) => ['parametric', shape]),
    ['text', '5'], ['text', '4'], ['text', '8'], ['text', 'TARA'],
  ];

  it('spaces every formation at least FORMATION_SPACING apart', () => {
    // The sequencer's collision-free morphs depend on this floor.
    for (const [type, payload] of all) {
      for (const n of [2, 10, 50, 100]) {
        expect(minPair(buildFormation(type, payload, n)), `${payload}/${n}`)
          .toBeGreaterThanOrEqual(FORMATION_SPACING - 1e-9);
      }
    }
  });

  it('centres every formation over the fleet\'s pad grid and above the ground', () => {
    for (const [type, payload] of all) {
      for (const n of [10, 100]) {
        const pts = buildFormation(type, payload, n);
        const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
        const c = fleetCenter(n);
        expect((Math.min(...xs) + Math.max(...xs)) / 2, `${payload}/${n}`).toBeCloseTo(c.x, 6);
        expect((Math.min(...ys) + Math.max(...ys)) / 2, `${payload}/${n}`).toBeCloseTo(c.y, 6);
        for (const p of pts) expect(p.z, `${payload}/${n}`).toBeLessThan(0);
      }
    }
  });

  it('stacks a crowded flat shape instead of growing it without bound', () => {
    // Equal-angle sampling used to put two heart drones 0.15 m apart at 50
    // drones; scaling that up to the floor made the heart ~900 m across.
    for (const shape of ['heart', 'grid', 'line', 'vee']) {
      const pts = buildFormation('parametric', shape, 100);
      const c = fleetCenter(100);
      const reach = Math.max(...pts.map((p) => Math.hypot(p.x - c.x, p.y - c.y)));
      expect(reach, shape).toBeLessThanOrEqual(14 + 1e-6);
    }
  });
});

describe('SHAPE_LIBRARY', () => {
  it('lists every parametric shape, SVG assets, plus the ten digits', () => {
    expect(SHAPE_LIBRARY.filter((e) => e.shapeType === 'parametric')).toHaveLength(SHAPES.length);
    expect(SHAPE_LIBRARY.filter((e) => e.shapeType === 'svg')).toHaveLength(2);
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

describe('formation minimum spacing at scale > 1 (#136)', () => {
  const minPair = (pts) => {
    let m = Infinity;
    for (let i = 0; i < pts.length; i++) {
      for (let j = i + 1; j < pts.length; j++) {
        m = Math.min(m, Math.hypot(pts[i].x - pts[j].x, pts[i].y - pts[j].y, pts[i].z - pts[j].z));
      }
    }
    return m;
  };

  it('resolves the S0 benchmark failures on line, vee, and circle at scale > 1', () => {
    const knownCases = [
      { n: 60, shape: 'line', scales: [1.5, 2.0, 3.0] },
      { n: 60, shape: 'vee', scales: [1.5, 2.0, 3.0] },
      { n: 100, shape: 'line', scales: [1.5, 2.0, 3.0] },
      { n: 100, shape: 'vee', scales: [1.5, 2.0, 3.0] },
      { n: 100, shape: 'circle', scales: [1.5, 2.0, 3.0] },
    ];

    for (const { n, shape, scales } of knownCases) {
      for (const scale of scales) {
        const pts = buildFormation('parametric', shape, n, scale);
        expect(pts).toHaveLength(n);
        const d = minPair(pts);
        expect(d, `${shape}@N=${n}, scale=${scale}`).toBeGreaterThanOrEqual(2.0 - 1e-6);
        const reach = pts.reduce((m, p) => Math.max(m, Math.hypot(p.x, p.y)), 0);
        expect(reach, `${shape}@N=${n}, scale=${scale} reach`).toBeLessThanOrEqual(30.0 + 1e-6);
        const maxAlt = Math.max(...pts.map((p) => -p.z));
        expect(maxAlt, `${shape}@N=${n}, scale=${scale} altitude`).toBeLessThanOrEqual(120.0 + 1e-6);
      }
    }
  });

  it('guarantees design separation (>= 2.0m) across all supported parametric shapes up to scale 2.0', () => {
    for (const shape of SHAPES) {
      for (const n of [10, 25, 50, 75, 100]) {
        for (const scale of [0.5, 1.0, 1.5, 2.0]) {
          const pts = buildFormation('parametric', shape, n, scale);
          expect(pts).toHaveLength(n);
          expect(minPair(pts), `${shape}@N=${n}, scale=${scale}`).toBeGreaterThanOrEqual(2.0 - 1e-6);
        }
      }
    }
  });

  it('rejects with explicit actionable error when formation constraints cannot be met', () => {
    // When an extreme scale would push stacked layers beyond the altitude ceiling
    expect(() => buildFormation('parametric', 'line', 100, 20.0)).toThrow(/exceeds maximum altitude ceiling/);
    expect(() => buildFormation('parametric', 'heart', 75, 3.0)).toThrow(/exceeds maximum altitude ceiling/);
  });
});

describe('text formations keep the design spacing or are refused', () => {
  // #136 guaranteed the spacing for parametric shapes only, so lettering at a
  // large fleet was shrunk to fit the site and built without any complaint:
  // NAVRATRI at 500 drones came out 0.11 m apart, HAPPY NAVRATRI at 40 at 1.86 m.
  const minSpacing = (pts) => {
    let m = Infinity;
    for (let i = 0; i < pts.length; i++) {
      for (let j = i + 1; j < pts.length; j++) {
        m = Math.min(m, Math.hypot(pts[i].x - pts[j].x, pts[i].y - pts[j].y, pts[i].z - pts[j].z));
      }
    }
    return m;
  };

  it.each([
    ['NAVRATRI', 500],
    ['NAVRATRI', 50],
    ['HAPPY NAVRATRI', 40],
    ['GANPATI', 100],
    ['0', 500],
  ])('refuses %s at %i drones instead of building it under 2.0 m', (text, n) => {
    expect(() => buildFormation('text', text, n)).toThrow(/minimum spacing/);
  });

  it('never returns lettering closer than the design spacing', () => {
    for (const text of ['5', '42', 'TARA', 'NAVRATRI', 'HAPPY NAVRATRI']) {
      for (const n of [2, 12, 30, 40, 60, 100, 200]) {
        let pts;
        try {
          pts = buildFormation('text', text, n);
        } catch (e) {
          expect(e.message, `${text}@${n}`).toMatch(/minimum spacing/);
          continue;
        }
        expect(minSpacing(pts), `${text}@${n}`).toBeGreaterThanOrEqual(MIN_SEPARATION_M - 1e-6);
      }
    }
  });

  it('still builds the lettering that fits: NAVRATRI up to 40 drones, HAPPY NAVRATRI up to 30', () => {
    for (const [text, n] of [['NAVRATRI', 40], ['HAPPY NAVRATRI', 30], ['5', 100]]) {
      expect(minSpacing(buildFormation('text', text, n)), `${text}@${n}`).toBeGreaterThanOrEqual(MIN_SEPARATION_M - 1e-6);
    }
  });

  it('fails the whole show build, so Studio reports it instead of previewing it', () => {
    const scenes = [makeScene({ shapeType: 'text', shapePayload: 'NAVRATRI', holdDurationSeconds: 4, transitionDurationSeconds: 4 })];
    expect(() => buildSequencedShow(scenes, 500, 4)).toThrow(/minimum spacing/);
  });
});
