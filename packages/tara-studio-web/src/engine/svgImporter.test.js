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
import { buildFormation } from './formations.js';
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

  it('parses valid translate, scale, rotate, and matrix transforms', () => {
    const fn1 = parseTransform('translate(10, 20)');
    expect(fn1({ x: 5, y: 5 })).toEqual({ x: 15, y: 25 });

    const fn2 = parseTransform('scale(2, 3)');
    expect(fn2({ x: 4, y: 5 })).toEqual({ x: 8, y: 15 });

    const fn3 = parseTransform('translate(10, -5) scale(2)');
    expect(fn3({ x: 1, y: 2 })).toEqual({ x: 12, y: -1 });

    const fnRot = parseTransform('rotate(90)');
    const pRot = fnRot({ x: 10, y: 0 });
    expect(pRot.x).toBeCloseTo(0);
    expect(pRot.y).toBeCloseTo(10);

    const fnRotOrigin = parseTransform('rotate(90, 5, 5)');
    const pRotO = fnRotOrigin({ x: 10, y: 5 });
    expect(pRotO.x).toBeCloseTo(5);
    expect(pRotO.y).toBeCloseTo(10);

    const fnMat = parseTransform('matrix(2, 0, 0, 3, 10, 20)');
    expect(fnMat({ x: 1, y: 2 })).toEqual({ x: 12, y: 26 });
  });

  it('rejects unsupported transforms explicitly', () => {
    expect(() => parseTransform('skewX(10)')).toThrow(/unsupported/i);
    expect(() => parseTransform('skewY(20)')).toThrow(/unsupported/i);
    expect(() => parseTransform('matrix(1, 2, 3)')).toThrow(/parameter count|invalid/i);
    expect(() => parseTransform('translate()')).toThrow(/parameter count|malformed/i);
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

  it('rejects a bare number after Z instead of hanging (self-review, pre-merge)', () => {
    // 'Z' takes no parameters and cannot be implicitly repeated the way
    // M/L/H/V/C/Q can; a bare coordinate right after it previously left
    // the command unchanged and never advanced past the token, hanging
    // the parser forever instead of rejecting the input.
    expect(() => parsePathCommands('M 0 0 L 1 1 Z 2 2')).toThrow(/does not take parameters/i);
  });

  it('continues a subpath from the closepath point when a command follows Z without an explicit M (self-review, pre-merge)', () => {
    // Per the SVG spec, a drawing command right after a closepath starts a
    // new subpath at that same point rather than an empty one. The
    // previous code cleared curContour on Z without reseeding it, so the
    // following command's own endpoint was the only point in that
    // subpath - one point is below the >= 2 threshold that makes it into
    // `contours`, so the whole segment silently vanished.
    const contours = parsePathCommands('M 0 0 L 1 1 Z L 2 2');
    expect(contours).toHaveLength(2);
    expect(contours[1]).toEqual([{ x: 0, y: 0 }, { x: 2, y: 2 }]);
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

describe('svgImporter: strict semantics, nested group transforms and viewBox mapping (PR #146)', () => {
  it('correctly inherits nested group transforms (<g transform="...">)', () => {
    const svg = `
      <svg viewBox="0 0 100 100">
        <g transform="translate(10, 20)">
          <g transform="scale(2)">
            <path d="M 0 0 L 10 10" />
          </g>
        </g>
      </svg>
    `;
    const contours = parseSvgToContours(svg);
    expect(contours).toHaveLength(1);
    expect(contours[0][0]).toEqual({ x: 10, y: 20 });
    expect(contours[0][1]).toEqual({ x: 30, y: 40 });
  });

  it('correctly applies group rotation and scaling', () => {
    const svg = `
      <svg viewBox="0 0 100 100">
        <g transform="rotate(90) scale(2)">
          <path d="M 10 0 L 20 0" />
        </g>
      </svg>
    `;
    const contours = parseSvgToContours(svg);
    expect(contours).toHaveLength(1);
    expect(contours[0][0].x).toBeCloseTo(0);
    expect(contours[0][0].y).toBeCloseTo(20);
    expect(contours[0][1].x).toBeCloseTo(0);
    expect(contours[0][1].y).toBeCloseTo(40);
  });

  it('evaluates chained transform combinations in correct order', () => {
    const svg = `
      <svg viewBox="0 0 100 100">
        <path d="M 0 0 L 10 0" transform="translate(10, 20) rotate(90) scale(2)" />
      </svg>
    `;
    const contours = parseSvgToContours(svg);
    expect(contours[0][0]).toEqual({ x: 10, y: 20 });
    expect(contours[0][1].x).toBeCloseTo(10);
    expect(contours[0][1].y).toBeCloseTo(40);
  });

  it('correctly maps nonzero viewBox origins into viewport space', () => {
    const svg = `
      <svg viewBox="50 100 200 200">
        <path d="M 50 100 L 150 200" />
      </svg>
    `;
    const contours = parseSvgToContours(svg);
    expect(contours[0][0]).toEqual({ x: 0, y: 0 });
    expect(contours[0][1]).toEqual({ x: 100, y: 100 });
  });

  it('handles preserveAspectRatio="none" with non-uniform stretching', () => {
    const svg = `
      <svg viewBox="0 0 100 100" width="200" height="100" preserveAspectRatio="none">
        <path d="M 0 0 L 50 50" />
      </svg>
    `;
    const contours = parseSvgToContours(svg);
    expect(contours[0][0]).toEqual({ x: 0, y: 0 });
    expect(contours[0][1]).toEqual({ x: 100, y: 50 });
  });

  it('rejects unsupported preserveAspectRatio="slice"', () => {
    const svg = '<svg viewBox="0 0 100 100" preserveAspectRatio="xMidYMid slice"><path d="M 0 0 L 10 10" /></svg>';
    expect(() => parseSvgToContours(svg)).toThrow(/slice/i);
  });

  it('rejects unsupported geometry-producing tags explicitly', () => {
    expect(() => parseSvgToContours('<svg><rect x="0" y="0" width="10" height="10"/></svg>')).toThrow(/unsupported svg element '<rect>'/i);
    expect(() => parseSvgToContours('<svg><circle cx="5" cy="5" r="5"/></svg>')).toThrow(/unsupported svg element '<circle>'/i);
    expect(() => parseSvgToContours('<svg><ellipse cx="5" cy="5" rx="5" ry="3"/></svg>')).toThrow(/unsupported svg element '<ellipse>'/i);
    expect(() => parseSvgToContours('<svg><line x1="0" y1="0" x2="10" y2="10"/></svg>')).toThrow(/unsupported svg element '<line>'/i);
  });

  it('buildFormation rejects invalid SVG input fail-closed and never converts to a circle', () => {
    // Malformed SVG
    expect(() => buildFormation('svg', '<svg><invalid_syntax>', 10)).toThrow();
    // Unsupported element
    expect(() => buildFormation('svg', '<svg><rect width="10" height="10"/></svg>', 10)).toThrow(/unsupported svg element/i);
    // Degenerate geometry
    expect(() => buildFormation('svg', '<svg><path d="M 0 0 L 0 0"/></svg>', 10)).toThrow(/degenerate/i);
  });

  it('scales Dandiya assets to representative fleet sizes (N=24, 40) within site radius and spacing limits', () => {
    for (const n of [24, 40]) {
      const pts = svgToFormation('dandiya_sticks', n);
      expect(pts).toHaveLength(n);
      for (const p of pts) {
        expect(Number.isFinite(p.x)).toBe(true);
        expect(Number.isFinite(p.y)).toBe(true);
        expect(Number.isFinite(p.z)).toBe(true);
        expect(Math.hypot(p.x, p.y)).toBeLessThanOrEqual(SITE_RADIUS_M + 1e-4);
        expect(-p.z).toBeGreaterThan(0);
        expect(-p.z).toBeLessThanOrEqual(MAX_ALTITUDE_M);
      }
      let minD = Infinity;
      for (let i = 0; i < pts.length; i++) {
        for (let j = i + 1; j < pts.length; j++) {
          const d = Math.hypot(pts[i].x - pts[j].x, pts[i].y - pts[j].y, pts[i].z - pts[j].z);
          if (d < minD) minD = d;
        }
      }
      expect(minD, `Spacing at N=${n}`).toBeGreaterThanOrEqual(MIN_DESIGN_SPACING_M - 1e-4);
    }
  });

  it('rejects Dandiya assets when drone count exceeds site boundary constraints', () => {
    expect(() => svgToFormation('dandiya_sticks', 100)).toThrow(/exceeding site limits/i);
  });
});

describe('svgImporter: hidden and non-rendered geometry (issue #122/#146 review)', () => {
  // A visible square (bbox 10..60) plus a decoy square (bbox 70..95) that no
  // renderer would ever draw - mirrors the 10-23 release review's own fixture.
  const visibleSquare = 'M 10 10 L 60 10 L 60 60 L 10 60 Z';
  const decoySquare = 'M 70 70 L 95 70 L 95 95 L 70 95 Z';
  const bbox = (contours) => {
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const c of contours) {
      for (const p of c) {
        minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x);
        minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y);
      }
    }
    return { minX, maxX, minY, maxY };
  };
  const onlyTheVisibleSquare = { minX: 10, maxX: 60, minY: 10, maxY: 60 };

  it('skips geometry inside <defs> - only <use> ever draws it, and <use> is forbidden', () => {
    const svg = `<svg viewBox="0 0 100 100">
      <path d="${visibleSquare}" />
      <defs><path d="${decoySquare}" /></defs>
    </svg>`;
    const contours = parseSvgToContours(svg);
    expect(contours).toHaveLength(1);
    expect(bbox(contours)).toEqual(onlyTheVisibleSquare);
  });

  it.each([
    ['display="none"', 'display="none"'],
    ['style="display:none"', 'style="display: none"'],
    ['visibility="hidden"', 'visibility="hidden"'],
    ['opacity="0"', 'opacity="0"'],
  ])('skips a decoy element hidden with %s', (_label, attr) => {
    const svg = `<svg viewBox="0 0 100 100">
      <path d="${visibleSquare}" />
      <path d="${decoySquare}" ${attr} />
    </svg>`;
    const contours = parseSvgToContours(svg);
    expect(contours).toHaveLength(1);
    expect(bbox(contours)).toEqual(onlyTheVisibleSquare);
  });

  it('skips a decoy element hidden with style="opacity:0"', () => {
    const svg = `<svg viewBox="0 0 100 100">
      <path d="${visibleSquare}" />
      <path d="${decoySquare}" style="opacity:0" />
    </svg>`;
    const contours = parseSvgToContours(svg);
    expect(contours).toHaveLength(1);
    expect(bbox(contours)).toEqual(onlyTheVisibleSquare);
  });

  it('does not treat stroke-opacity:0 or fill-opacity:0 as hiding the element (self-review, pre-merge)', () => {
    // The style regex for opacity:0 previously had no word boundary before
    // "opacity", so it also matched inside stroke-opacity:0 or
    // fill-opacity:0 - hiding a path whose fill (or stroke) was still
    // visible, dropping real geometry from the import with no error. This
    // path's fill is red, not none, so it still renders despite its
    // invisible stroke and must not be dropped.
    const svg = `<svg viewBox="0 0 100 100">
      <path d="${visibleSquare}" />
      <path d="${decoySquare}" style="fill:red;stroke-opacity:0" />
    </svg>`;
    const contours = parseSvgToContours(svg);
    expect(contours).toHaveLength(2);
  });

  it('skips every child of a hidden group (<g display="none">), including nested groups inside it', () => {
    const svg = `<svg viewBox="0 0 100 100">
      <path d="${visibleSquare}" />
      <g display="none">
        <g><path d="${decoySquare}" /></g>
      </g>
    </svg>`;
    const contours = parseSvgToContours(svg);
    expect(contours).toHaveLength(1);
    expect(bbox(contours)).toEqual(onlyTheVisibleSquare);
  });

  it('still rejects an unsupported element when it would actually render', () => {
    expect(() => parseSvgToContours('<svg><path d="M 0 0 L 10 10" /><rect x="70" y="70" width="25" height="25"/></svg>'))
      .toThrow(/unsupported svg element '<rect>'/i);
  });

  it('does not reject an unsupported element when it is hidden - it can never render either way', () => {
    const svg = `<svg viewBox="0 0 100 100">
      <path d="${visibleSquare}" />
      <rect x="70" y="70" width="25" height="25" display="none" />
    </svg>`;
    expect(() => parseSvgToContours(svg)).not.toThrow();
  });
});

describe('svgImporter: self-closing detection regression (2026-10-26 release review)', () => {
  // A greedy [^>]* always captures a trailing self-closing '/' as part of
  // attrsStr - the match already succeeds with a trailing (\/?) group
  // empty, and a regex only backtracks when the match would otherwise fail.
  // Reading isSelfClosing off that group was always false, so a self-closing
  // hidden element or <defs/> suppressed everything after it forever
  // (nothing ever decremented suppressDepth), and a self-closing <g
  // transform> stayed pushed on the matrix stack and leaked onto every
  // sibling. These three cases, plus a non-self-closing control, are the
  // ones the earlier hidden-geometry tests happened not to exercise: every
  // decoy there was the LAST element in its SVG, so nothing downstream could
  // reveal that everything after it had gone missing.
  const square = (x0) => `M ${x0} 10 L ${x0 + 20} 10 L ${x0 + 20} 30 L ${x0} 30 Z`;

  it('a self-closing hidden element does not swallow every sibling after it', () => {
    const svg = `<svg viewBox="0 0 100 100">
      <path d="${square(10)}" />
      <path d="${square(40)}" display="none" />
      <path d="${square(70)}" />
    </svg>`;
    const contours = parseSvgToContours(svg);
    expect(contours).toHaveLength(2);
  });

  it('a non-self-closing hidden element (explicit close tag) is still just itself skipped - control', () => {
    const svg = `<svg viewBox="0 0 100 100">
      <path d="${square(10)}" />
      <g display="none"><path d="${square(40)}" /></g>
      <path d="${square(70)}" />
    </svg>`;
    const contours = parseSvgToContours(svg);
    expect(contours).toHaveLength(2);
  });

  it('a self-closing, empty <g transform> does not leak its transform onto later siblings', () => {
    const svg = `<svg viewBox="0 0 100 100">
      <g transform="translate(30,0)" />
      <path d="M 10 10 L 40 10" />
    </svg>`;
    const contours = parseSvgToContours(svg);
    expect(contours[0][0].x).toBe(10);
    expect(contours[0][1].x).toBe(40);
  });

  it('a self-closing <defs/> does not suppress every element after it', () => {
    const svg = '<svg viewBox="0 0 100 100"><defs/><path d="M 10 10 L 40 10" /></svg>';
    const contours = parseSvgToContours(svg);
    expect(contours).toHaveLength(1);
    expect(contours[0][0].x).toBe(10);
  });
});

describe('svgImporter: nested <svg> rejection (issue #122/#146 review)', () => {
  it('rejects a nested <svg transform="...">, rather than letting it mutate the base transform', () => {
    const svg = `<svg viewBox="0 0 200 200">
      <path d="M 10 10 L 20 10" />
      <svg transform="translate(50,0)"><path d="M 70 70 L 95 70" /></svg>
    </svg>`;
    expect(() => parseSvgToContours(svg)).toThrow(/nested <svg>/i);
  });

  it('rejects a nested <svg> even with no transform of its own', () => {
    const svg = '<svg viewBox="0 0 100 100"><path d="M 0 0 L 10 10" /><svg><path d="M 20 20 L 30 30" /></svg></svg>';
    expect(() => parseSvgToContours(svg)).toThrow(/nested <svg>/i);
  });
});

describe('svgImporter: linear-time tag scan (issue #122/#146 review)', () => {
  it('fails fast on a long unterminated tag at the 500,000-character size limit, instead of backtracking quadratically', () => {
    // The previous tagRegex took ~0.57s at 50k unterminated characters and
    // 7.9s at 200k (release review, 2026-10-23) - quadratic growth that
    // extrapolates to tens of seconds at this 500,000-character ceiling.
    const unterminated = `<tag${' '.repeat(499_990)}`;
    expect(unterminated.length).toBeLessThanOrEqual(500_000);
    const start = performance.now();
    expect(() => parseSvgToContours(unterminated)).toThrow();
    expect(performance.now() - start).toBeLessThan(2000);
  });
});
