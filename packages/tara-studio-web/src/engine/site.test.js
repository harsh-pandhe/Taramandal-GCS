import { describe, expect, it } from 'vitest';
import { SITE_PROFILES, SITE_NAMES, STANDARD_SITE, siteProfile } from './site.js';
import { PAD_COLS, PAD_SPACING, padAt, hoverAt, fleetCenter } from './layout.js';
import { buildFormation, SITE_RADIUS_M } from './formations.js';
import { svgToFormation } from './svgImporter.js';
import { buildSequencedShow, computeAssignmentChainLocal, computeShowDuration, makeScene } from './sequencer.js';
import { ShowRefusalError } from './showErrors.js';

// A flat figure of `n` points on a ring of the given radius in the x-z plane, so its horizontal reach is `radius`.
const ring = (n, radius, alt = 60) => JSON.stringify(Array.from({ length: n }, (_, i) => {
  const a = (2 * Math.PI * i) / n;
  return { x: radius * Math.cos(a), y: 0, z: -(alt + radius * Math.sin(a)) };
}));

describe('site profiles', () => {
  it('standard is the 30 m site with 13 pad columns every earlier show was built on', () => {
    expect(STANDARD_SITE).toBe('standard');
    expect(siteProfile(undefined)).toBe(SITE_PROFILES.standard);
    expect(SITE_PROFILES.standard).toMatchObject({ radius_m: 30, padCols: 13 });
    expect(SITE_RADIUS_M).toBe(30);
    expect(PAD_COLS).toBe(13);
  });

  it('scale400 is the 60 m site with 27 pad columns', () => {
    expect(SITE_PROFILES.scale400).toMatchObject({ radius_m: 60, padCols: 27 });
    expect(SITE_NAMES).toEqual(['standard', 'scale400']);
  });

  it('pad columns are floor(radius / pad spacing), the same rule as tara_core.launch_grid', () => {
    for (const p of Object.values(SITE_PROFILES)) expect(p.padCols).toBe(Math.floor(p.radius_m / PAD_SPACING));
  });

  it('an unknown site throws, it is never guessed', () => {
    expect(() => siteProfile('huge')).toThrow(/unknown site profile/);
    expect(() => padAt(0, 'huge')).toThrow(/unknown site profile/);
  });
});

describe('pad layout by site', () => {
  it('the default and "standard" are identical to the layout before profiles', () => {
    for (const i of [0, 1, 12, 13, 14, 100, 399, 999]) {
      expect(padAt(i)).toEqual(padAt(i, 'standard'));
      expect(hoverAt(i)).toEqual(hoverAt(i, 'standard'));
    }
    expect(padAt(13)).toEqual({ x: PAD_SPACING, y: 0, z: 0 });
    expect(fleetCenter(400)).toEqual(fleetCenter(400, 'standard'));
  });

  it('scale400 is 27 columns wide: drones 0..12 do not move, 13 and above do', () => {
    for (let i = 0; i < 13; i++) expect(padAt(i, 'scale400')).toEqual(padAt(i));
    expect(padAt(13, 'scale400')).toEqual({ x: 0, y: 13 * PAD_SPACING, z: 0 });
    expect(padAt(27, 'scale400').x).toBeCloseTo(PAD_SPACING);
    expect(padAt(27, 'scale400').y).toBe(0);
  });

  it('adjacent pads still hover at different heights on a wider grid', () => {
    const level = (i) => hoverAt(i, 'scale400').z;
    for (const i of [0, 1, 26, 27, 28, 53, 54]) {
      if ((i + 1) % 27 !== 0) expect(level(i)).not.toBe(level(i + 1));
      expect(level(i)).not.toBe(level(i + 27));
    }
  });
});

describe('the site radius in the formation builders', () => {
  const FIGURE_REACHING_45_M = ring(40, 45);

  it('an explicit formation reaching 45 m is refused on the standard site and accepted on scale400', () => {
    expect(() => buildFormation('exact_points', FIGURE_REACHING_45_M, 40)).toThrow(ShowRefusalError);
    expect(() => buildFormation('exact_points', FIGURE_REACHING_45_M, 40, 1.0, 'standard')).toThrow(/site radius \(30m\)/);
    const pts = buildFormation('exact_points', FIGURE_REACHING_45_M, 40, 1.0, 'scale400');
    expect(pts).toHaveLength(40);
  });

  it('scale400 still refuses anything beyond 60 m', () => {
    expect(() => buildFormation('exact_points', ring(40, 61), 40, 1.0, 'scale400')).toThrow(/site radius \(60m\)/);
    expect(buildFormation('exact_points', ring(40, 59.9), 40, 1.0, 'scale400')).toHaveLength(40);
  });

  it('a built-in formation is unchanged by passing the standard site explicitly', () => {
    expect(buildFormation('parametric', 'circle', 50, 1.0, 'standard')).toEqual(buildFormation('parametric', 'circle', 50, 1.0));
  });

  it('the SVG importer takes the same site', () => {
    const svg = '<svg viewBox="0 0 100 100"><path d="M 0 0 L 100 0 L 100 100 L 0 100 Z" fill="none" stroke="black"/></svg>';
    // 400 drones need a span of about 4 x 100 x ... beyond 60 m in one contour; the point is that the limit quoted is the site's.
    expect(() => svgToFormation(svg, 400, { scale: 20 })).toThrow(/site limits \(30m radius\)/);
    expect(() => svgToFormation(svg, 400, { scale: 20, site: 'scale400' })).toThrow(/site limits \(60m radius\)|requires/);
  });
});

describe('a whole show on a site', () => {
  const scenes = () => [makeScene({ name: 'A', shapeType: 'exact_points', shapePayload: ring(20, 40), holdDurationSeconds: 4, transitionDurationSeconds: 8 })];

  it('a show on the standard site is built exactly as before', () => {
    const base = buildSequencedShow([makeScene({ name: 'c', shapeType: 'parametric', shapePayload: 'circle', holdDurationSeconds: 3, transitionDurationSeconds: 6 })], 20, 10);
    const same = buildSequencedShow([makeScene({ name: 'c', shapeType: 'parametric', shapePayload: 'circle', holdDurationSeconds: 3, transitionDurationSeconds: 6 })], 20, 10, null, 'standard', 'standard');
    expect(same.drones.map((d) => d.waypoints.length)).toEqual(base.drones.map((d) => d.waypoints.length));
    expect(same.drones[3].waypoints[0]).toEqual(base.drones[3].waypoints[0]);
  });

  it('a show on scale400 starts and ends on that site\'s pads', () => {
    const n = 20;
    expect(() => computeAssignmentChainLocal(scenes(), n)).toThrow(ShowRefusalError); // 40 m reach: refused on standard
    const chain = computeAssignmentChainLocal(scenes(), n, 'scale400');
    const show = buildSequencedShow(scenes(), n, 10, chain, 'standard', 'scale400');
    for (const i of [0, 7, 19]) {
      const pad = padAt(i, 'scale400');
      const first = show.drones[i].waypoints[0], last = show.drones[i].waypoints.at(-1);
      expect(first.x).toBeCloseTo(pad.x, 6);
      expect(first.y).toBeCloseTo(pad.y, 6);
      expect(last.x).toBeCloseTo(pad.x, 6);
      expect(last.y).toBeCloseTo(pad.y, 6);
    }
    expect(computeShowDuration(scenes(), n, 10, chain, 'standard', 'scale400')).toBeCloseTo(show._duration, 1);
  });
});
