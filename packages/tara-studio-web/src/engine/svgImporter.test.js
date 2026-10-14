import { describe, it, expect } from 'vitest';
import {
  sanitizeSvg,
  validateViewBox,
  parseTransform,
  parsePathCommands,
  parseSvgToContours,
  sampleSvgContours,
  svgToFormation,
  SITE_RADIUS_M,
  MAX_ALTITUDE_M,
  MIN_DESIGN_SPACING_M,
} from './svgImporter.js';
import {
  buildSequencedShow,
  makeScene,
  computeAssignmentChainLocal,
} from './sequencer.js';

describe('svgImporter: sanitizeSvg security controls', () => {
  it('rejects empty or whitespace-only SVG', () => {
    expect(() => sanitizeSvg('')).toThrow(/empty/i);
    expect(() => sanitizeSvg('   ')).toThrow(/empty/i);
    expect(() => sanitizeSvg(null)).toThrow(/empty/i);
  });

  it('rejects oversized SVGs exceeding 500 KB', () => {
    const hugeSvg = '<svg>' + ' '.repeat(500_001) + '</svg>';
    expect(() => sanitizeSvg(hugeSvg)).toThrow(/exceeds maximum size/i);
  });

  it('rejects embedded script elements', () => {
    const malicious = '<svg><script>alert("pwned")</script><path d="M0 0L10 10"/></svg>';
    expect(() => sanitizeSvg(malicious)).toThrow(/forbidden pattern/i);
  });

  it('rejects foreignObject, iframe, embed, and image elements', () => {
    expect(() => sanitizeSvg('<svg><foreignObject>evil</foreignObject></svg>')).toThrow(/forbidden pattern/i);
    expect(() => sanitizeSvg('<svg><iframe src="https://evil.com"></iframe></svg>')).toThrow(/forbidden pattern/i);
    expect(() => sanitizeSvg('<svg><embed src="evil.swf"/></svg>')).toThrow(/forbidden pattern/i);
    expect(() => sanitizeSvg('<svg><image href="http://evil.com/x.png"/></svg>')).toThrow(/forbidden pattern/i);
  });

  it('rejects inline event handlers', () => {
    expect(() => sanitizeSvg('<svg onload="alert(1)"><circle r="5"/></svg>')).toThrow(/forbidden pattern/i);
    expect(() => sanitizeSvg('<path onclick="fetch()" d="M0 0"/>')).toThrow(/forbidden pattern/i);
    expect(() => sanitizeSvg('<svg onmouseover="steal()"><rect/></svg>')).toThrow(/forbidden pattern/i);
  });

  it('rejects external entity (XXE) and DOCTYPE declarations', () => {
    const xxe = '<!DOCTYPE svg [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]><svg>&xxe;</svg>';
    expect(() => sanitizeSvg(xxe)).toThrow(/forbidden pattern/i);
    expect(() => sanitizeSvg('<!DOCTYPE html><html></html>')).toThrow(/forbidden pattern/i);
  });

  it('rejects external URL links in href/xlink:href', () => {
    expect(() => sanitizeSvg('<svg><a href="http://malicious.com"><path d="M0 0"/></a></svg>')).toThrow(/forbidden pattern/i);
    expect(() => sanitizeSvg('<svg><a xlink:href="https://evil.com"><path d="M0 0"/></a></svg>')).toThrow(/forbidden pattern/i);
    expect(() => sanitizeSvg('<svg><a href="javascript:alert(1)"><path d="M0 0"/></a></svg>')).toThrow(/forbidden pattern/i);
  });

  it('accepts safe vector markup', () => {
    const safe = '<svg viewBox="0 0 100 100"><path d="M 10 10 L 90 90" stroke="black"/></svg>';
    expect(sanitizeSvg(safe)).toBe(safe);
  });
});

describe('svgImporter: viewBox and coordinate transform parsing', () => {
  it('validates correct viewBox dimensions', () => {
    const vb = validateViewBox('<svg viewBox="0 0 100 200"></svg>');
    expect(vb).toEqual({ minX: 0, minY: 0, width: 100, height: 200 });
  });

  it('rejects degenerate or negative viewBox dimensions', () => {
    expect(() => validateViewBox('<svg viewBox="0 0 -10 100"></svg>')).toThrow(/positive/i);
    expect(() => validateViewBox('<svg viewBox="0 0 100 0"></svg>')).toThrow(/positive/i);
    expect(() => validateViewBox('<svg viewBox="0 0 100"></svg>')).toThrow(/expected 4/i);
  });

  it('parses valid translate and scale transforms', () => {
    const fn1 = parseTransform('translate(10, 20)');
    expect(fn1({ x: 5, y: 5 })).toEqual({ x: 15, y: 25 });

    const fn2 = parseTransform('scale(2, 3)');
    expect(fn2({ x: 4, y: 5 })).toEqual({ x: 8, y: 15 });

    const fn3 = parseTransform('translate(10, -5) scale(2)');
    expect(fn3({ x: 1, y: 2 })).toEqual({ x: 12, y: -1 });
  });

  it('rejects unsupported transforms explicitly', () => {
    expect(() => parseTransform('rotate(45)')).toThrow(/unsupported/i);
    expect(() => parseTransform('matrix(1, 0, 0, 1, 0, 0)')).toThrow(/unsupported/i);
    expect(() => parseTransform('skewX(10)')).toThrow(/unsupported/i);
  });
});

describe('svgImporter: parsePathCommands syntax and robustness', () => {
  it('parses basic M, L, H, V, Z path commands', () => {
    const d = 'M 10 20 L 30 40 H 50 V 60 Z';
    const contours = parsePathCommands(d);
    expect(contours).toHaveLength(1);
    expect(contours[0].length).toBeGreaterThanOrEqual(4);
    expect(contours[0][0]).toEqual({ x: 10, y: 20 });
    expect(contours[0][1]).toEqual({ x: 30, y: 40 });
    expect(contours[0][2]).toEqual({ x: 50, y: 40 });
    expect(contours[0][3]).toEqual({ x: 50, y: 60 });
  });

  it('correctly parses negative numbers and scientific exponent notation', () => {
    const d = 'M -1.5e1 2.5e-1 L +3.0E+0 -4.2e-2 Z';
    const contours = parsePathCommands(d);
    expect(contours).toHaveLength(1);
    expect(contours[0][0].x).toBeCloseTo(-15.0);
    expect(contours[0][0].y).toBeCloseTo(0.25);
    expect(contours[0][1].x).toBeCloseTo(3.0);
    expect(contours[0][1].y).toBeCloseTo(-0.042);
  });

  it('parses relative path commands (m, l, h, v, c, q, z)', () => {
    const d = 'm 10 10 l 10 0 v 10 h -10 z';
    const contours = parsePathCommands(d);
    expect(contours).toHaveLength(1);
    expect(contours[0].length).toBeGreaterThanOrEqual(4);
  });

  it('parses cubic and quadratic bezier curves', () => {
    const cubic = parsePathCommands('M 0 0 C 10 20 20 20 30 0');
    expect(cubic[0].length).toBeGreaterThan(6);

    const quad = parsePathCommands('M 0 0 Q 15 30 30 0');
    expect(quad[0].length).toBeGreaterThan(5);
  });

  it('rejects unsupported path commands explicitly instead of silently skipping', () => {
    expect(() => parsePathCommands('M 0 0 A 25 25 0 0 1 50 25')).toThrow(/unsupported svg path command 'A'/i);
    expect(() => parsePathCommands('M 0 0 S 10 20 30 40')).toThrow(/unsupported svg path command 'S'/i);
    expect(() => parsePathCommands('M 0 0 T 50 50')).toThrow(/unsupported svg path command 'T'/i);
  });

  it('rejects malformed paths with missing coordinates or unexpected tokens', () => {
    expect(() => parsePathCommands('M 10')).toThrow(/missing coordinate/i);
    expect(() => parsePathCommands('M 10 20 L 30')).toThrow(/missing coordinate/i);
    expect(() => parsePathCommands('M 10 20 C 1 2 3 4 5')).toThrow(/missing coordinate/i);
    expect(() => parsePathCommands('M 10 20 invalid_token 30')).toThrow(/unexpected token/i);
    expect(() => parsePathCommands('L 10 20')).toThrow(/must start with moveto/i);
  });
});

describe('svgImporter: parseSvgToContours geometry parsing', () => {
  it('parses polygons and polyline elements', () => {
    const svg = '<svg><polygon points="0,0 10,0 10,10 0,10" /></svg>';
    const contours = parseSvgToContours(svg);
    expect(contours).toHaveLength(1);
    expect(contours[0]).toHaveLength(4);
  });

  it('applies supported transform attribute on path geometry', () => {
    const svg = '<svg><path d="M 0 0 L 10 10" transform="translate(20, 30)" /></svg>';
    const contours = parseSvgToContours(svg);
    expect(contours[0][0]).toEqual({ x: 20, y: 30 });
    expect(contours[0][1]).toEqual({ x: 30, y: 40 });
  });

  it('rejects SVG with no valid drawable geometry', () => {
    expect(() => parseSvgToContours('<svg><g></g></svg>')).toThrow(/no valid drawable paths/i);
  });
});

describe('svgImporter: sampleSvgContours resampling', () => {
  it('samples exactly N points from contours', () => {
    const contours = [[{ x: 0, y: 0 }, { x: 100, y: 0 }]];
    for (const n of [1, 5, 20, 50]) {
      const sampled = sampleSvgContours(contours, n, 20.0);
      expect(sampled).toHaveLength(n);
    }
  });

  it('throws on degenerate zero-length geometry', () => {
    const contours = [[{ x: 5, y: 5 }, { x: 5, y: 5 }]];
    expect(() => sampleSvgContours(contours, 10, 20.0)).toThrow(/degenerate/i);
  });
});

describe('svgImporter: svgToFormation 3D point cloud generation and site limits', () => {
  const minPairDist = (pts) => {
    let m = Infinity;
    for (let i = 0; i < pts.length; i++) {
      for (let j = i + 1; j < pts.length; j++) {
        const d = Math.hypot(pts[i].x - pts[j].x, pts[i].y - pts[j].y, pts[i].z - pts[j].z);
        if (d < m) m = d;
      }
    }
    return m;
  };

  it('resolves built-in Dandiya Sticks asset within canonical 30m site limits and >= 2.0 m spacing', () => {
    const pts = svgToFormation('dandiya_sticks', 24, { altitude: 15.0 });
    expect(pts).toHaveLength(24);
    for (const p of pts) {
      expect(Number.isFinite(p.x)).toBe(true);
      expect(Number.isFinite(p.y)).toBe(true);
      expect(Number.isFinite(p.z)).toBe(true);
      expect(Math.hypot(p.x, p.y)).toBeLessThanOrEqual(SITE_RADIUS_M + 1e-4);
      expect(-p.z).toBeGreaterThan(0);
      expect(-p.z).toBeLessThanOrEqual(MAX_ALTITUDE_M);
    }
    const dMin = minPairDist(pts);
    expect(dMin).toBeGreaterThanOrEqual(MIN_DESIGN_SPACING_M - 1e-4);
  });

  it('resolves built-in Diya Flame asset and satisfies spacing and site bounds', () => {
    const pts = svgToFormation('diya_flame', 30, { altitude: 18.0 });
    expect(pts).toHaveLength(30);
    for (const p of pts) {
      expect(Math.hypot(p.x, p.y)).toBeLessThanOrEqual(SITE_RADIUS_M + 1e-4);
      expect(-p.z).toBeGreaterThan(0);
      expect(-p.z).toBeLessThanOrEqual(MAX_ALTITUDE_M);
    }
    const dMin = minPairDist(pts);
    expect(dMin).toBeGreaterThanOrEqual(MIN_DESIGN_SPACING_M - 1e-4);
  });

  it('supports horizontal plane XZ vs vertical billboard XY', () => {
    const singleLineSvg = '<svg><path d="M 0 0 L 20 20" /></svg>';
    const xy = svgToFormation(singleLineSvg, 6, { plane: 'XY', altitude: 15.0 });
    const xz = svgToFormation(singleLineSvg, 6, { plane: 'XZ', altitude: 15.0 });

    expect(xy.every((p) => Math.abs(p.y) < 1e-4)).toBe(true);
    expect(xz.every((p) => Math.abs(p.z - -15.0) < 1e-4)).toBe(true);
  });

  it('produces pure deterministic output for identical calls', () => {
    const a = svgToFormation('dandiya_sticks', 20, { scale: 15.0 });
    const b = svgToFormation('dandiya_sticks', 20, { scale: 15.0 });
    expect(a).toEqual(b);
  });

  it('rejects when fleet count requires span exceeding 30m site boundary', () => {
    const tinySvg = '<svg><path d="M 0 0 L 1 0" /></svg>';
    expect(() => svgToFormation(tinySvg, 500)).toThrow(/exceeding site limits \(30m radius\)/i);
  });
});

describe('svgImporter: deterministic integration with normal editable scenes and safety validator', () => {
  const minSeparation = (show) => {
    let m = Infinity;
    const w = show.drones.map((d) => d.waypoints);
    for (let k = 0; k < w[0].length; k++) {
      for (let i = 0; i < w.length; i++) {
        for (let j = i + 1; j < w.length; j++) {
          const a = w[i][k], b = w[j][k];
          m = Math.min(m, Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z));
        }
      }
    }
    return m;
  };

  it('converts Dandiya fixtures into normal editable scenes and passes full trajectory safety validation', () => {
    const scenes = [
      makeScene({
        name: 'Dandiya Sticks',
        shapeType: 'svg',
        shapePayload: 'dandiya_sticks',
        holdDurationSeconds: 6,
        transitionDurationSeconds: 4,
      }),
      makeScene({
        name: 'Diya Flame',
        shapeType: 'svg',
        shapePayload: 'diya_flame',
        holdDurationSeconds: 6,
        transitionDurationSeconds: 4,
      }),
    ];

    for (const n of [5, 16, 24, 40]) {
      const idx = computeAssignmentChainLocal(scenes, n);
      const show = buildSequencedShow(scenes, n, 4, idx);

      expect(show.drones).toHaveLength(n);
      expect(show._duration).toBeGreaterThan(20);

      // Trajectory separation check across entire show
      const sep = minSeparation(show);
      expect(sep, `Dandiya show separation at n=${n}`).toBeGreaterThanOrEqual(MIN_DESIGN_SPACING_M);

      // Horizontal and vertical site limits across all waypoints
      for (const drone of show.drones) {
        for (const wp of drone.waypoints) {
          const reach = Math.hypot(wp.x, wp.y);
          expect(reach, `Waypoint horizontal reach at n=${n}`).toBeLessThanOrEqual(SITE_RADIUS_M + 1e-4);
          const alt = -wp.z;
          expect(alt, `Waypoint altitude at n=${n}`).toBeGreaterThanOrEqual(-1e-4);
          expect(alt, `Waypoint altitude ceiling at n=${n}`).toBeLessThanOrEqual(MAX_ALTITUDE_M);
        }
      }
    }
  });
});
