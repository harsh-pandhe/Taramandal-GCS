import { describe, it, expect } from 'vitest';
import { buildDepthCopies, MIN_SEPARATION_M, SITE_RADIUS_M } from './formations.js';

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

// A digit built this small is a flat, single-depth-layer wall (fmtText
// stacks into extra layers only when a tall/wide string needs it) with a
// small enough footprint on its own to leave real room for several depth
// copies within the 30m site radius - exactly the realistic case this
// primitive targets (a flat imported outline, not a shape like 'grid' or
// 'circle' that already spans the whole site on its own).
const WALL = ['text', '1', 5];

describe('buildDepthCopies', () => {
  it('returns exactly totalCount points', () => {
    const [shapeType, payload, perCopy] = WALL;
    const result = buildDepthCopies(shapeType, payload, perCopy, perCopy * 3 + 2);
    expect(result).toHaveLength(perCopy * 3 + 2);
  });

  it('every full copy exactly matches the front copy from the audience (y, z unchanged)', () => {
    const [shapeType, payload, perCopy] = WALL;
    const result = buildDepthCopies(shapeType, payload, perCopy, perCopy * 3);
    const front = result.slice(0, perCopy);
    for (let c = 1; c < 3; c++) {
      const copy = result.slice(c * perCopy, (c + 1) * perCopy);
      for (let i = 0; i < perCopy; i++) {
        expect(copy[i].y).toBeCloseTo(front[i].y, 9);
        expect(copy[i].z).toBeCloseTo(front[i].z, 9);
      }
    }
  });

  it('the last, partial copy is an exact subset of the front copy, not regenerated', () => {
    const [shapeType, payload, perCopy] = WALL;
    const total = perCopy * 2 + 2; // 2 full copies + 2 leftover
    const result = buildDepthCopies(shapeType, payload, perCopy, total);
    expect(result).toHaveLength(total);
    const front = result.slice(0, perCopy);
    const partial = result.slice(2 * perCopy);
    expect(partial).toHaveLength(2);
    for (let i = 0; i < partial.length; i++) {
      expect(partial[i].y).toBeCloseTo(front[i].y, 9);
      expect(partial[i].z).toBeCloseTo(front[i].z, 9);
    }
  });

  it('keeps every pair across different copies at least depthSpacing apart, and at least MIN_SEPARATION_M overall', () => {
    const [shapeType, payload, perCopy] = WALL;
    const result = buildDepthCopies(shapeType, payload, perCopy, perCopy * 3);
    let crossCopyMin = Infinity;
    for (let a = 0; a < 3; a++) {
      for (let b = a + 1; b < 3; b++) {
        const groupA = result.slice(a * perCopy, (a + 1) * perCopy);
        const groupB = result.slice(b * perCopy, (b + 1) * perCopy);
        for (const p of groupA) for (const q of groupB) {
          const d = Math.hypot(p.x - q.x, p.y - q.y, p.z - q.z);
          if (d < crossCopyMin) crossCopyMin = d;
        }
      }
    }
    expect(crossCopyMin).toBeGreaterThanOrEqual(2.2 - 1e-6);
    expect(minPairDist(result)).toBeGreaterThanOrEqual(MIN_SEPARATION_M - 1e-6);
  });

  it('accounts for a front copy that already spans multiple depth layers on its own', () => {
    // A large scale forces fmtText to stack itself into more than one X
    // layer internally (TEXT_MAX_HEIGHT) even for a short string - the exact
    // case the frontSpanX term exists for. Confirm it really has a nonzero
    // span, then check the cross-copy guarantee still holds against the
    // real points, not just the formula.
    const payload = '1';
    const perCopy = 8;
    const scale = 8;
    const front = buildDepthCopies('text', payload, perCopy, perCopy, scale); // single copy == buildFormation
    const frontSpanX = Math.max(...front.map((p) => p.x)) - Math.min(...front.map((p) => p.x));
    expect(frontSpanX).toBeGreaterThan(0.01);

    const result = buildDepthCopies('text', payload, perCopy, perCopy * 2, scale);
    const groupA = result.slice(0, perCopy);
    const groupB = result.slice(perCopy);
    let crossCopyMin = Infinity;
    for (const p of groupA) for (const q of groupB) {
      const d = Math.hypot(p.x - q.x, p.y - q.y, p.z - q.z);
      if (d < crossCopyMin) crossCopyMin = d;
    }
    expect(crossCopyMin).toBeGreaterThanOrEqual(2.2 - 1e-6);
  });

  it('respects a custom depthSpacing when the front copy has zero depth span', () => {
    const [shapeType, payload, perCopy] = WALL;
    const result = buildDepthCopies(shapeType, payload, perCopy, perCopy * 2, 1.0, 3.5);
    const front = result.slice(0, perCopy);
    const second = result.slice(perCopy);
    expect(second[0].x - front[0].x).toBeCloseTo(3.5, 6);
  });

  it('is deterministic', () => {
    const [shapeType, payload, perCopy] = WALL;
    expect(buildDepthCopies(shapeType, payload, perCopy, perCopy * 3 + 2))
      .toEqual(buildDepthCopies(shapeType, payload, perCopy, perCopy * 3 + 2));
  });

  it('centres the combined X-extent at 0, even when the front copy on its own would not be (issue #169 review)', () => {
    // perCopyCount=5 (WALL) happens to have its own fleetCenter(5).x === 0
    // (the pad grid fits in one row, <=PAD_COLS), which let an earlier
    // version of this test pass while the underlying centring math was
    // still wrong for any count that spans more than one row. 100 does
    // span more than one row, so fleetCenter(100).x is nonzero - this is
    // the case that actually exercises the fix.
    const perCopy = 30;
    const result = buildDepthCopies('text', '1', perCopy, perCopy * 2);
    const xs = result.map((p) => p.x);
    expect((Math.min(...xs) + Math.max(...xs)) / 2).toBeCloseTo(0, 6);
  });

  it('centres correctly even when the final copy is a partial, not full, copy (self-review, pre-merge)', () => {
    // The previous centring formula assumed every copy spans the front
    // formation's full X-extent, which is only true for full copies. With
    // perCopyCount=3 and totalCount=7, the 3rd copy holds only front[0]
    // (a 1-point fragment, not the full 3-point span), so deriving the
    // shift from frontSpanX/copiesNeeded alone (as the old code did)
    // mis-centred the real combined cloud by a non-trivial amount. Centring
    // must be measured from the actual placed points, not assumed.
    const result = buildDepthCopies('parametric', 'line', 3, 7, 1, 2.2);
    const xs = result.map((p) => p.x);
    expect((Math.min(...xs) + Math.max(...xs)) / 2).toBeCloseTo(0, 6);
  });

  it('every point stays within the site radius and under the altitude ceiling', () => {
    const [shapeType, payload, perCopy] = WALL;
    const result = buildDepthCopies(shapeType, payload, perCopy, perCopy * 3);
    for (const p of result) {
      expect(Math.hypot(p.x, p.y)).toBeLessThanOrEqual(SITE_RADIUS_M + 1e-6);
      expect(-p.z).toBeLessThanOrEqual(120.0 + 1e-6);
    }
  });

  it('refuses rather than compress spacing when too many copies would exceed the site radius', () => {
    // A base formation that's already large on its own (a 40-drone grid
    // spans the site), repeated several times, cannot fit at any
    // depthSpacing >= MIN_SEPARATION_M.
    expect(() => buildDepthCopies('parametric', 'grid', 40, 40 * 5))
      .toThrow(/site radius/);
  });

  it('rejects a non-positive or non-integer perCopyCount or totalCount', () => {
    for (const bad of [0, -1, 1.5, NaN]) {
      expect(() => buildDepthCopies('parametric', 'grid', bad, 20)).toThrow(/perCopyCount/);
      expect(() => buildDepthCopies('parametric', 'grid', 20, bad)).toThrow(/totalCount/);
    }
  });

  it('rejects a depthSpacing below MIN_SEPARATION_M', () => {
    expect(() => buildDepthCopies('parametric', 'grid', 20, 40, 1.0, 1.0)).toThrow(/depthSpacing/);
  });

  it('totalCount === perCopyCount is exactly one copy, equal to buildFormation itself', () => {
    const [shapeType, payload, perCopy] = WALL;
    const result = buildDepthCopies(shapeType, payload, perCopy, perCopy);
    expect(result).toHaveLength(perCopy);
    for (const p of result) {
      expect(Math.hypot(p.x, p.y)).toBeLessThanOrEqual(SITE_RADIUS_M + 1e-6);
    }
  });

  it('rejects totalCount < perCopyCount rather than silently returning a fragment of one copy (issue #169 review)', () => {
    // Before this fix, buildDepthCopies('text', '1', 15, 5) returned 5
    // points of a 15-point shape - a fragment of the outline, not a
    // smaller version of it. A caller wanting fewer drones than one copy
    // holds wants buildFormation directly.
    const [shapeType, payload, perCopy] = WALL;
    expect(() => buildDepthCopies(shapeType, payload, perCopy + 10, perCopy))
      .toThrow(/totalCount.*less than perCopyCount/);
  });

  it('a realistic figure at a scale the built-in generators can actually represent fairly', () => {
    // Most of this file deliberately uses a tiny 5-drone wall so the
    // combined arrangement comfortably clears the site radius; this one
    // checks the mechanism still holds together at a size closer to what
    // M1's real use (once #146 lands) will actually need. 80/copy, not
    // literally the 100+ the review asked for: even text('1', 100) alone
    // already reaches ~23m from origin (checked directly), leaving no
    // room for a second copy at any depthSpacing within the 30m site -
    // the same "built-in generators aren't thin enough at this scale"
    // finding already noted in the PR body, not a new gap.
    const perCopy = 80;
    const result = buildDepthCopies('text', '1', perCopy, perCopy * 2);
    expect(result).toHaveLength(perCopy * 2);
    expect(minPairDist(result)).toBeGreaterThanOrEqual(MIN_SEPARATION_M - 1e-6);
    for (const p of result) {
      expect(Math.hypot(p.x, p.y)).toBeLessThanOrEqual(SITE_RADIUS_M + 1e-6);
    }
  });
});
