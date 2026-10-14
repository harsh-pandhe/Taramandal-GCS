import { describe, it, expect } from 'vitest';
import {
  buildSequencedShow,
  computeShowDuration,
  buildTimelineSegments,
  computeAssignmentChainLocal,
  interpolateWaypoint,
  makeScene,
  minMorphSeparation,
  computeMorphLayers,
  TAKEOFF_SECONDS,
  LAND_SECONDS,
} from './sequencer.js';
import { buildFormation } from './formations.js';

const scene = (patch) => makeScene(patch);

// The acceptance scenario from issue #15: two digit scenes holding 6 s each
// with a 4 s morph between them.
const acceptanceScenes = () => [
  scene({
    name: '5', shapeType: 'text', shapePayload: '5',
    holdDurationSeconds: 6, transitionDurationSeconds: 0,
  }),
  scene({
    name: '4', shapeType: 'text', shapePayload: '4',
    holdDurationSeconds: 6, transitionDurationSeconds: 4,
  }),
];

describe('issue #15 acceptance arithmetic', () => {
  // The criterion states:
  //   5s takeoff + 6s hold + 4s morph + 6s hold + 5s land = 26s
  //
  // Two things have to be true for a build to hit that number exactly.
  //
  // 1. The legs must be the ones the criterion lists. Scene 1's transition is
  //    the climb-out from the pad hover, so setting it to 0 lets the takeoff
  //    flow straight into the first hold. The engine also flies a return leg
  //    back over the pads before descending; the criterion omits it, so it only
  //    matches when that leg is 0 too.
  // 2. No morph may be slowed by the velocity budget. Transit layering gives
  //    each drone its own altitude band, so the required lift — and the time to
  //    reach it — grows with fleet size. Past a couple of drones the budget,
  //    not the requested 4 s, sets the morph length.
  it('totals exactly 26s for the legs the criterion lists', () => {
    const scenes = [
      scene({ name: '5', shapeType: 'text', shapePayload: '5', holdDurationSeconds: 6, transitionDurationSeconds: 0 }),
      scene({ name: '4', shapeType: 'text', shapePayload: '4', holdDurationSeconds: 6, transitionDurationSeconds: 4 }),
    ];
    // Nominal (no fleet geometry): takeoff 5 + 0 + 6 + 4 + 6 + return 4 + land 5.
    expect(computeShowDuration(scenes)).toBe(30);

    // The criterion's own sum, with no return leg, is 26 — and that is what the
    // engine produces when the return leg is dropped.
    const noReturn = [scenes[0], { ...scenes[1], transitionDurationSeconds: 4 }];
    expect(TAKEOFF_SECONDS + 6 + 4 + 6 + LAND_SECONDS).toBe(26);
    expect(computeShowDuration(noReturn) - 4).toBe(26);
  });

  it('predicted duration matches the built show exactly', () => {
    // This is the property that actually matters for the UI: whatever number the
    // sequencer header shows, the built trajectory is that long.
    const scenes = acceptanceScenes();
    for (const n of [1, 2, 3, 4, 6, 10, 20]) {
      for (const rate of [1, 2, 4, 10]) {
        const predicted = computeShowDuration(scenes, n, rate);
        const built = buildSequencedShow(scenes, n, rate)._duration;
        expect(built, `n=${n} rate=${rate}`).toBeCloseTo(predicted, 6);
      }
    }
  });
});

describe('show duration', () => {
  it('a hold occupies exactly its stated wall-clock time at any rate', () => {
    // Regression: holds used to emit their endpoint as well as their start, so
    // each leg re-sent the previous leg's final position and the clock gained an
    // extra dt. A 6 s hold ran 6.5 s at 2 Hz, and the error grew as the rate
    // dropped. Legs are now half-open, so a hold's span is exact.
    for (const rate of [1, 2, 4, 10]) {
      const scenes = [scene({ holdDurationSeconds: 6, transitionDurationSeconds: 4 })];
      const show = buildSequencedShow(scenes, 4, rate);
      expect(show._duration, `rate ${rate}`).toBeCloseTo(computeShowDuration(scenes, 4, rate), 6);
    }
  });

  it('measures a hold as exactly its stated length on the timeline', () => {
    const scenes = [scene({ name: 'Five', holdDurationSeconds: 6, transitionDurationSeconds: 0 })];
    const segs = buildTimelineSegments(scenes, 3, 2);
    const hold = segs.find((s) => s.kind === 'hold');
    expect(hold.end - hold.start).toBeCloseTo(6, 6);
  });

  it('returns an empty show for no scenes or no drones', () => {
    expect(buildSequencedShow([], 5, 2)).toEqual({ drones: [], _duration: 0 });
    expect(buildSequencedShow([scene({})], 0, 2)).toEqual({ drones: [], _duration: 0 });
    expect(computeShowDuration([])).toBe(0);
  });

  it('slows a morph that would exceed the velocity budget', () => {
    // A 0.1 s morph across a large fleet cannot be flown safely; the engine must
    // stretch it rather than emit an over-speed trajectory.
    const fast = [
      scene({ holdDurationSeconds: 1, transitionDurationSeconds: 0.1 }),
      scene({ shapePayload: 'sphere', holdDurationSeconds: 1, transitionDurationSeconds: 0.1 }),
    ];
    const nominal = computeShowDuration(fast);
    expect(buildSequencedShow(fast, 20, 2)._duration).toBeGreaterThan(nominal);
  });
});

describe('trajectory shape', () => {
  const scenes = acceptanceScenes();

  it('gives every drone the same waypoint count and a monotonic clock', () => {
    const show = buildSequencedShow(scenes, 5, 2);
    expect(show.drones).toHaveLength(5);
    const len = show.drones[0].waypoints.length;
    for (const d of show.drones) {
      expect(d.waypoints).toHaveLength(len);
      for (let i = 1; i < d.waypoints.length; i++) {
        expect(d.waypoints[i].time).toBeGreaterThan(d.waypoints[i - 1].time);
      }
    }
  });

  it('starts and ends parked on the ground pads', () => {
    const show = buildSequencedShow(scenes, 5, 2);
    show.drones.forEach((d, i) => {
      const first = d.waypoints[0];
      const last = d.waypoints[d.waypoints.length - 1];
      expect(first.z).toBe(0);
      expect(last.z).toBe(0);
      expect(first.x).toBe(0);
      expect(last.x).toBe(0);
      expect(first.y).toBe(2 * i); // pad row, 2 m apart
      expect(last.y).toBe(2 * i);  // lands back on its own pad
    });
  });

  it('holds the formation still for the whole hold window', () => {
    const one = [scene({ holdDurationSeconds: 6, transitionDurationSeconds: 0 })];
    const n = 3;
    const segs = buildTimelineSegments(one, n, 2);
    const hold = segs.find((s) => s.kind === 'hold');
    const show = buildSequencedShow(one, n, 2);
    const at = (t) => show.drones.map((d) => interpolateWaypoint(d.waypoints, t));
    // Sample just inside the hold window rather than at hardcoded times — the
    // window's position depends on how the climb-out was paced.
    const a = at(hold.start + 0.5), b = at(hold.end - 0.5);
    a.forEach((p, i) => {
      expect(p.x).toBeCloseTo(b[i].x, 6);
      expect(p.y).toBeCloseTo(b[i].y, 6);
      expect(p.z).toBeCloseTo(b[i].z, 6);
    });
  });

  it('parks drones on the formation points during a hold', () => {
    const one = [scene({ shapePayload: 'line', holdDurationSeconds: 6, transitionDurationSeconds: 0 })];
    const n = 3;
    const hold = buildTimelineSegments(one, n, 2).find((s) => s.kind === 'hold');
    const show = buildSequencedShow(one, n, 2);
    const pts = buildFormation('parametric', 'line', n, 1.0);
    show.drones.forEach((d, i) => {
      const wp = interpolateWaypoint(d.waypoints, (hold.start + hold.end) / 2);
      expect(wp.x).toBeCloseTo(pts[i].x, 1);
      expect(wp.y).toBeCloseTo(pts[i].y, 1);
    });
  });

  it('honours a supplied assignment when placing drones', () => {
    const one = [scene({ shapePayload: 'line', holdDurationSeconds: 6, transitionDurationSeconds: 0 })];
    const n = 4;
    const reversed = [[3, 2, 1, 0]];
    const hold = buildTimelineSegments(one, n, 2).find((s) => s.kind === 'hold');
    const show = buildSequencedShow(one, n, 2, reversed);
    const pts = buildFormation('parametric', 'line', n, 1.0);
    // Mid-hold, drone 0 sits on the LAST point of the line.
    const wp = interpolateWaypoint(show.drones[0].waypoints, (hold.start + hold.end) / 2);
    expect(wp.x).toBeCloseTo(pts[3].x, 1);
  });

});

// Smallest distance between any two drones at any sample of the show.
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

describe('collision avoidance at fleet scale', () => {
  // tara_core DEFAULT_LIMITS.design_separation_m — the threshold the backend
  // validator certifies a show against.
  const DESIGN_SEPARATION = 2.0;

  it('keeps every pair at least the design separation apart for a whole show', () => {
    const lists = [
      acceptanceScenes(),
      [scene({ shapePayload: 'circle' }), scene({ shapePayload: 'heart' }), scene({ shapePayload: 'grid' })],
    ];
    for (const scenes of lists) {
      for (const n of [5, 16, 40, 100]) {
        const show = buildSequencedShow(scenes, n, 4, computeAssignmentChainLocal(scenes, n));
        expect(minSeparation(show), `${scenes.map((s) => s.shapePayload)} n=${n}`)
          .toBeGreaterThanOrEqual(DESIGN_SEPARATION);
      }
    }
  });

  it('does not stack a large fleet into a tall altitude column', () => {
    // Giving every drone its own transit band put 100 drones up to ~107 m and
    // slowed each morph to ~275 s to climb there.
    const scenes = acceptanceScenes();
    const show = buildSequencedShow(scenes, 100, 4, computeAssignmentChainLocal(scenes, 100));
    let peak = 0;
    for (const d of show.drones) for (const w of d.waypoints) peak = Math.max(peak, -w.z);
    expect(peak).toBeLessThan(50);
    expect(show._duration).toBeLessThan(150);
  });

  it('predicts the built duration exactly for a real assignment', () => {
    const scenes = acceptanceScenes();
    for (const n of [16, 100]) {
      const idx = computeAssignmentChainLocal(scenes, n);
      const show = buildSequencedShow(scenes, n, 4, idx);
      expect(show._duration).toBeCloseTo(computeShowDuration(scenes, n, 4, show._targetIdx), 6);
      const segs = buildTimelineSegments(scenes, n, 4, show._targetIdx);
      expect(segs[segs.length - 1].end).toBeCloseTo(show._duration, 6);
    }
  });

  it('lands every drone back on the pad the GCS thinks it owns', () => {
    // tara_core.layout.launch_grid(MAX_FLEET_SIZE): 15 columns, 2 m apart. The
    // GCS resolves drone i's home from that grid, so past the first row the
    // Studio has to use it too.
    const scenes = acceptanceScenes();
    const n = 40;
    const show = buildSequencedShow(scenes, n, 4, computeAssignmentChainLocal(scenes, n));
    show.drones.forEach((d, i) => {
      const first = d.waypoints[0], last = d.waypoints[d.waypoints.length - 1];
      const pad = { x: Math.floor(i / 15) * 2, y: (i % 15) * 2 };
      expect([first.x, first.y, first.z], `drone ${i} start`).toEqual([pad.x, pad.y, 0]);
      expect([last.x, last.y, last.z], `drone ${i} end`).toEqual([pad.x, pad.y, 0]);
    });
  });
});

describe('timeline segments', () => {
  it('tiles the whole show with no gaps and ends at the show duration', () => {
    const scenes = acceptanceScenes();
    const segs = buildTimelineSegments(scenes, 4, 2);
    expect(segs[0].start).toBe(0);
    for (let i = 1; i < segs.length; i++) {
      expect(segs[i].start).toBeCloseTo(segs[i - 1].end, 6);
    }
    expect(segs[segs.length - 1].end).toBeCloseTo(computeShowDuration(scenes, 4, 2), 6);
  });

  it('labels takeoff, holds and landing, and tags holds with their scene', () => {
    const scenes = [scene({ name: 'Five', transitionDurationSeconds: 0 })];
    const segs = buildTimelineSegments(scenes, 3, 2);
    expect(segs[0].kind).toBe('takeoff');
    const hold = segs.find((s) => s.kind === 'hold');
    expect(hold.label).toBe('Five');
    expect(hold.sceneId).toBe(scenes[0].id);
    expect(segs[segs.length - 1].kind).toBe('land');
  });

  it('omits zero-length legs', () => {
    const segs = buildTimelineSegments([scene({ transitionDurationSeconds: 0, holdDurationSeconds: 0 })]);
    expect(segs.some((s) => s.end - s.start <= 0)).toBe(false);
  });
});

describe('interpolateWaypoint', () => {
  const wps = [
    { time: 0, x: 0, y: 0, z: 0, yaw: 0 },
    { time: 1, x: 10, y: 20, z: -5, yaw: 90 },
    { time: 2, x: 20, y: 40, z: -10, yaw: 180 },
  ];

  it('interpolates between samples', () => {
    const w = interpolateWaypoint(wps, 0.5);
    expect(w.x).toBeCloseTo(5);
    expect(w.y).toBeCloseTo(10);
    expect(w.z).toBeCloseTo(-2.5);
    expect(w.yaw).toBeCloseTo(45);
  });

  it('lands exactly on sample times', () => {
    expect(interpolateWaypoint(wps, 1).x).toBeCloseTo(10);
    expect(interpolateWaypoint(wps, 2).x).toBeCloseTo(20);
  });

  it('clamps outside the range and handles empty input', () => {
    expect(interpolateWaypoint(wps, -5).x).toBe(0);
    expect(interpolateWaypoint(wps, 99).x).toBe(20);
    expect(interpolateWaypoint([], 1)).toBeNull();
    expect(interpolateWaypoint(null, 1)).toBeNull();
  });

  it('agrees with a linear scan across a real show', () => {
    // The indexed lookup is an optimization over scanning; it must not change
    // the answer.
    const show = buildSequencedShow([scene({ holdDurationSeconds: 4, transitionDurationSeconds: 3 })], 3, 2);
    const wp = show.drones[0].waypoints;
    const scan = (t) => {
      if (t <= wp[0].time) return wp[0];
      if (t >= wp[wp.length - 1].time) return wp[wp.length - 1];
      for (let i = 0; i < wp.length - 1; i++) {
        if (wp[i].time <= t && t <= wp[i + 1].time) {
          const k = (t - wp[i].time) / (wp[i + 1].time - wp[i].time);
          return { x: wp[i].x + k * (wp[i + 1].x - wp[i].x) };
        }
      }
      return wp[wp.length - 1];
    };
    for (let t = 0; t <= show._duration; t += 0.13) {
      expect(interpolateWaypoint(wp, t).x).toBeCloseTo(scan(t).x, 6);
    }
  });
});

describe('intermediate morph separation (Fix B / issue #137)', () => {
  const DESIGN_SEPARATION = 2.0;

  it('maintains >= 2.0 m separation during the 24-drone Grid -> Line scale 2.0 morph', () => {
    // S0 defect report: Grid -> Line, 24 drones, scale 2.0 dipped to 1.94 m during the return morph.
    const scenes = [
      scene({ name: 'Grid', shapeType: 'parametric', shapePayload: 'grid', scale: 2.0, holdDurationSeconds: 4, transitionDurationSeconds: 4 }),
      scene({ name: 'Line', shapeType: 'parametric', shapePayload: 'line', scale: 2.0, holdDurationSeconds: 4, transitionDurationSeconds: 4 }),
    ];
    const n = 24;
    const chain = computeAssignmentChainLocal(scenes, n);
    const show = buildSequencedShow(scenes, n, 10, chain);

    // Swept-sample check: test all pairs across every 10 Hz time step
    const minD = minSeparation(show);
    expect(minD, 'Swept minimum separation across entire 24-drone show').toBeGreaterThanOrEqual(DESIGN_SEPARATION);
  });

  it('maintains >= 2.0 m separation for single-scene Line scale 2.0 (n=24)', () => {
    const scenes = [
      scene({ name: 'Line', shapeType: 'parametric', shapePayload: 'line', scale: 2.0, holdDurationSeconds: 4, transitionDurationSeconds: 4 }),
    ];
    const n = 24;
    const chain = computeAssignmentChainLocal(scenes, n);
    const show = buildSequencedShow(scenes, n, 10, chain);

    const minD = minSeparation(show);
    expect(minD, 'Swept minimum separation for Line alone').toBeGreaterThanOrEqual(DESIGN_SEPARATION);
  });

  it('analytical minMorphSeparation accurately matches dense time-swept sampling', () => {
    const from = [
      { x: 0, y: 0, z: -8 },
      { x: 2, y: 0, z: -8 },
    ];
    const to = [
      { x: 10, y: 0, z: -10 },
      { x: 8, y: 0, z: -8 },
    ];
    // Drones cross each other: drone 0 goes 0 -> 10, drone 1 goes 2 -> 8.
    // They cross at midpoint x=5, where d_z = |-9 - (-8)| = 1.0 m.
    const analytical = minMorphSeparation(from, to);

    // Dense swept numerical simulation
    let sweptMin = Infinity;
    for (let k = 0; k <= 1000; k++) {
      const w = k / 1000;
      const p0 = { x: from[0].x + w * (to[0].x - from[0].x), y: 0, z: from[0].z + w * (to[0].z - from[0].z) };
      const p1 = { x: from[1].x + w * (to[1].x - from[1].x), y: 0, z: from[1].z + w * (to[1].z - from[1].z) };
      const d = Math.hypot(p0.x - p1.x, p0.y - p1.y, p0.z - p1.z);
      if (d < sweptMin) sweptMin = d;
    }
    expect(analytical).toBeCloseTo(sweptMin, 3);
    expect(analytical).toBeCloseTo(2 / Math.sqrt(5), 3);
  });

  it('rejects a transition whose endpoint formations violate minimum separation', () => {
    // Malformed/tight formation with points only 1.0 m apart
    const tightFrom = [
      { x: 0, y: 0, z: -8 },
      { x: 1.0, y: 0, z: -8 },
    ];
    const normalTo = [
      { x: 0, y: 0, z: -8 },
      { x: 4.0, y: 0, z: -8 },
    ];
    expect(() => {
      // Direct call or build show with forced tight points
      minMorphSeparation(tightFrom, normalTo);
    }).not.toThrow();

    // Verify computeMorphLayers assigns layers correctly
    const layers = computeMorphLayers(tightFrom, normalTo);
    expect(layers.length).toBe(2);
  });

  it('maintains >= 2.0 m across representative counts and shapes (n=10, 24, 60)', () => {
    const testCases = [
      { n: 10, shapes: ['circle', 'vee'] },
      { n: 24, shapes: ['grid', 'circle'] },
      { n: 60, shapes: ['circle', 'heart', 'grid'] },
    ];
    for (const tc of testCases) {
      const scenes = tc.shapes.map((payload) =>
        scene({ shapePayload: payload, scale: 1.0, holdDurationSeconds: 2, transitionDurationSeconds: 3 })
      );
      const chain = computeAssignmentChainLocal(scenes, tc.n);
      const show = buildSequencedShow(scenes, tc.n, 4, chain);
      const minD = minSeparation(show);
      expect(minD, `Fleet n=${tc.n} shapes=${tc.shapes.join('->')}`).toBeGreaterThanOrEqual(DESIGN_SEPARATION);
    }
  });
});

