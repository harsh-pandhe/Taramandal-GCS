import { describe, it, expect } from 'vitest';
import {
  buildFormation, FORMATION_LABELS, SITE_RADIUS_M, MIN_SEPARATION_M,
} from './formations.js';

// Regression coverage for the Studio site-size fix: every generated
// formation used to be able to land well outside the 30m site radius
// (measured before this fix: ring 160m, sphere 149m, line 267m, vee 221m
// at N=100 — each shape offset by a fixed +n metres with no awareness of
// where the site boundary was). Every shape must now fit itself inside
// SITE_RADIUS_M intentionally (via auto-scaling and, where a single layer
// can't hold N drones at MIN_SEPARATION_M, additional altitude layers —
// not a post-hoc clamp), while never violating the minimum spacing or the
// altitude ceiling that would introduce.

const SHAPES = Object.keys(FORMATION_LABELS);
const REPRESENTATIVE_N = [1, 2, 5, 12, 25, 50, 75, 90, 100];
const MAX_ALTITUDE_M = 120.0; // mirrors tara_core.safety.DEFAULT_LIMITS.max_altitude_m

const horizontalReach = (pts) => pts.reduce((m, p) => Math.max(m, Math.hypot(p.x, p.y)), 0);

const minPairwiseDist3D = (pts) => {
  let best = Infinity;
  for (let i = 0; i < pts.length; i++) {
    for (let j = i + 1; j < pts.length; j++) {
      const d = Math.hypot(pts[i].x - pts[j].x, pts[i].y - pts[j].y, pts[i].z - pts[j].z);
      if (d < best) best = d;
    }
  }
  return pts.length > 1 ? best : Infinity;
};

describe('Studio 30m site limit', () => {
  it.each(SHAPES)('%s stays within SITE_RADIUS_M at every representative N', (shape) => {
    for (const n of REPRESENTATIVE_N) {
      const pts = buildFormation('parametric', shape, n, 1.0);
      expect(pts, `${shape}@N=${n}`).toHaveLength(n);
      expect(horizontalReach(pts), `${shape}@N=${n} reach`).toBeLessThanOrEqual(SITE_RADIUS_M + 1e-6);
    }
  });

  it.each(SHAPES)('%s keeps the design separation at every representative N', (shape) => {
    for (const n of REPRESENTATIVE_N) {
      const pts = buildFormation('parametric', shape, n, 1.0);
      if (pts.length < 2) continue;
      expect(minPairwiseDist3D(pts), `${shape}@N=${n} min separation`)
        .toBeGreaterThanOrEqual(MIN_SEPARATION_M - 1e-6);
    }
  });

  it.each(SHAPES)('%s stays within the altitude ceiling at every representative N', (shape) => {
    for (const n of REPRESENTATIVE_N) {
      const pts = buildFormation('parametric', shape, n, 1.0);
      for (const p of pts) {
        expect(-p.z, `${shape}@N=${n} altitude`).toBeGreaterThanOrEqual(0);
        expect(-p.z, `${shape}@N=${n} altitude`).toBeLessThanOrEqual(MAX_ALTITUDE_M);
      }
    }
  });

  it('text formations (including multi-character strings) stay within SITE_RADIUS_M at the design spacing, or are refused', () => {
    // A long string at a large N cannot fit the 30 m site at 2.0 m spacing.
    // Before, it was shrunk to fit anyway: GANPATI at N=100 built with drones
    // 0.57 m apart. Now it is refused, never built tighter than the spacing.
    let refused = 0;
    for (const payload of ['5', 'TARA', 'GANPATI', '00000000']) {
      for (const n of REPRESENTATIVE_N) {
        let pts;
        try {
          pts = buildFormation('text', payload, n, 1.0);
        } catch (e) {
          expect(e.message, `${payload}@N=${n}`).toMatch(/minimum spacing/);
          refused++;
          continue;
        }
        expect(pts, `${payload}@N=${n}`).toHaveLength(n);
        expect(horizontalReach(pts), `${payload}@N=${n} reach`).toBeLessThanOrEqual(SITE_RADIUS_M + 1e-6);
        expect(minPairwiseDist3D(pts), `${payload}@N=${n} spacing`).toBeGreaterThanOrEqual(MIN_SEPARATION_M - 1e-6);
      }
    }
    expect(refused, 'the long strings at large N must be refused').toBeGreaterThan(0);
  });

  it('flat shapes at small N stay on a single altitude — no needless layering', () => {
    // At N=12 none of the flat shapes (everything but the inherently 3-D
    // sphere) needs a second altitude layer or a shrunk spacing; this pins
    // that small-N behaviour doesn't regress into needless layering.
    for (const shape of SHAPES.filter((s) => s !== 'sphere')) {
      const pts = buildFormation('parametric', shape, 12, 1.0);
      const zs = new Set(pts.map((p) => p.z.toFixed(3)));
      expect(zs.size, `${shape} should not need a second altitude layer at N=12`).toBe(1);
    }
  });

  it('large N (ring) spills into additional altitude layers rather than exceeding the site radius', () => {
    const pts = buildFormation('parametric', 'circle', 100, 1.0);
    const zs = new Set(pts.map((p) => Math.round(-p.z)));
    expect(zs.size, 'circle@N=100 should use more than one altitude').toBeGreaterThan(1);
  });

  it('sphere stays genuinely 3-D — a single layer must not collapse onto one altitude', () => {
    // Regression for a real bug caught while building this fix: stackLayers
    // used to unconditionally overwrite every point's z with its own flat
    // layer altitude, discarding the sphere's own internal height variation
    // and silently turning it into a flat disc with far-too-tight spacing.
    for (const n of [8, 12, 50, 90]) {
      const pts = buildFormation('parametric', 'sphere', n, 1.0);
      const zs = new Set(pts.map((p) => p.z.toFixed(2)));
      expect(zs.size, `sphere@N=${n} should not collapse onto one altitude`).toBeGreaterThan(1);
    }
  });

  it('grid at N=100 stacks into altitude layers to stay within site radius under pad placement', () => {
    // Under 3.2m sequencer spacing and pad-grid centering ((6, 14) offset),
    // a flat 10x10 grid would reach ~35m from origin; stacking keeps it within 30m.
    const pts = buildFormation('parametric', 'grid', 100, 1.0);
    const zs = new Set(pts.map((p) => p.z.toFixed(3)));
    expect(zs.size).toBeGreaterThan(1);
    expect(horizontalReach(pts)).toBeLessThanOrEqual(SITE_RADIUS_M + 1e-6);
  });

  it('SITE_RADIUS_M and MIN_SEPARATION_M match the values documented as mirroring tara_core defaults', () => {
    // tara_core.safety.DEFAULT_LIMITS.geofence_radius_m and
    // .design_separation_m, respectively — see the comment in formations.js
    // for why these are deliberately-duplicated, not imported, constants.
    expect(SITE_RADIUS_M).toBe(30.0);
    expect(MIN_SEPARATION_M).toBe(2.0);
  });
});
