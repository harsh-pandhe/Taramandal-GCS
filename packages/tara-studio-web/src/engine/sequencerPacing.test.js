import { describe, it, expect } from 'vitest';
import { makeScene, buildSequencedShow, computeShowDuration, buildTimelineSegments, computeAssignmentChainLocal, PACING } from './sequencer.js';

const scenes = () => [
  makeScene({ name: 'ring', shapeType: 'parametric', shapePayload: 'circle', holdDurationSeconds: 3, transitionDurationSeconds: 4 }),
  makeScene({ name: 'grid', shapeType: 'parametric', shapePayload: 'grid', holdDurationSeconds: 3, transitionDurationSeconds: 4 }),
];

describe('pacing presets', () => {
  const n = 12;
  const sc = scenes();
  const chain = computeAssignmentChainLocal(sc, n);

  it('fast finishes the same show sooner than standard, and the prediction matches the build', () => {
    const std = buildSequencedShow(sc, n, 10, chain, 'standard');
    const fast = buildSequencedShow(sc, n, 10, chain, 'fast');
    expect(fast._duration).toBeLessThan(std._duration);
    expect(computeShowDuration(sc, n, 10, chain, 'fast')).toBe(fast._duration);
    expect(computeShowDuration(sc, n, 10, chain, 'standard')).toBe(std._duration);
    const segs = buildTimelineSegments(sc, n, 10, chain, 'fast');
    expect(segs[segs.length - 1].end).toBeCloseTo(fast._duration, 1);
  });

  it('flies the same path: every scene is reached at the same points, only sooner', () => {
    const std = buildSequencedShow(sc, n, 10, chain, 'standard');
    const fast = buildSequencedShow(sc, n, 10, chain, 'fast');
    const holdPoint = (show, label, drone) => {
      const seg = buildTimelineSegments(sc, n, 10, chain, show === std ? 'standard' : 'fast').find((s) => s.label === label);
      const wp = show.drones[drone].waypoints.find((w) => w.time >= seg.start + 0.5);
      return [wp.x, wp.y, wp.z];
    };
    for (const label of ['ring', 'grid']) {
      for (let d = 0; d < n; d++) expect(holdPoint(fast, label, d)).toEqual(holdPoint(std, label, d));
    }
  });

  it('never exceeds its own speed budget between samples, and stays under the certification limit', () => {
    const fast = buildSequencedShow(sc, n, 10, chain, 'fast');
    let vmax = 0;
    for (const d of fast.drones) {
      for (let i = 1; i < d.waypoints.length; i++) {
        const a = d.waypoints[i - 1], b = d.waypoints[i];
        vmax = Math.max(vmax, Math.hypot(b.x - a.x, b.y - a.y, b.z - a.z) / (b.time - a.time));
      }
    }
    expect(vmax).toBeLessThanOrEqual(PACING.fast.maxH + 0.3);
    expect(vmax).toBeLessThan(6.0);    // tara_core DEFAULT_LIMITS.max_velocity_ms
  });

  it('an unknown pacing falls back to standard', () => {
    expect(buildSequencedShow(sc, n, 10, chain, 'nonsense')._duration).toBe(buildSequencedShow(sc, n, 10, chain, 'standard')._duration);
  });
});

// PX4 (defaults) cannot fly faster than this, so a plan that asks for more is not flown as planned and
// the drones drift apart by different amounts (a 10-drone Fast show at 4 m/s down lagged by 15.9 m in
// Gazebo and tripped the proximity failsafe on 5 Nov). The planned path must stay inside the limits.
describe.each(['standard', 'fast'])('%s pacing stays inside the PX4 flight envelope', (pacing) => {
  const PX4_MAX_CLIMB = 3.0;    // MPC_Z_VEL_MAX_UP
  const PX4_MAX_DESCENT = 1.5;  // MPC_Z_VEL_MAX_DN
  const PX4_MAX_ACC_HOR = 3.0;  // MPC_ACC_HOR
  it('plans no climb above 3.0 m/s, no descent above 1.5 m/s and no horizontal acceleration above 3.0 m/s^2', () => {
    // long vertical legs are what hit the descent cap: layered morphs lift every drone tens of metres
    const sc = [
      makeScene({ name: 'ring', shapeType: 'parametric', shapePayload: 'circle', holdDurationSeconds: 3, transitionDurationSeconds: 1 }),
      makeScene({ name: 'grid', shapeType: 'parametric', shapePayload: 'grid', holdDurationSeconds: 3, transitionDurationSeconds: 1 }),
    ];
    const n = 60;
    const show = buildSequencedShow(sc, n, 10, computeAssignmentChainLocal(sc, n), pacing);
    let up = 0, down = 0, acc = 0;
    for (const d of show.drones) {
      const w = d.waypoints;
      const v = [];
      for (let i = 1; i < w.length; i++) {
        const dt = w[i].time - w[i - 1].time;
        const vx = (w[i].x - w[i - 1].x) / dt, vy = (w[i].y - w[i - 1].y) / dt, vz = -(w[i].z - w[i - 1].z) / dt;
        up = Math.max(up, vz); down = Math.max(down, -vz);
        v.push([vx, vy, dt]);
      }
      // 0.5 s window so 1 cm rounding does not read as acceleration
      for (let i = 5; i < v.length; i++) {
        const dtw = v.slice(i - 5, i).reduce((a, x) => a + x[2], 0);
        acc = Math.max(acc, Math.hypot(v[i][0] - v[i - 5][0], v[i][1] - v[i - 5][1]) / dtw);
      }
    }
    expect(up).toBeLessThanOrEqual(PX4_MAX_CLIMB);
    expect(down).toBeLessThanOrEqual(PX4_MAX_DESCENT);
    expect(acc).toBeLessThanOrEqual(PX4_MAX_ACC_HOR);
  });
});
