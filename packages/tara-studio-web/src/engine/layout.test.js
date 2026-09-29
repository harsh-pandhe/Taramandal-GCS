import { describe, it, expect } from 'vitest';
import { padAt, hoverAt, fleetCenter } from './layout.js';
import { solveAssignment } from './assignment.js';

describe('pad layout', () => {
  it('matches tara_core.layout.launch_grid(MAX_FLEET_SIZE)', () => {
    // launch_grid: cols = min(ceil(sqrt(1000)), floor(30 / 2)) = 15,
    // pad i = ((i // cols) * 2, (i % cols) * 2).
    for (let i = 0; i < 200; i++) {
      expect(padAt(i)).toEqual({ x: Math.floor(i / 15) * 2, y: (i % 15) * 2, z: 0 });
    }
  });

  it('hovers every pair at least 3.2 m apart', () => {
    const n = 150;
    let m = Infinity;
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const a = hoverAt(i), b = hoverAt(j);
        m = Math.min(m, Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z));
      }
    }
    expect(m).toBeGreaterThanOrEqual(3.2);
  });

  it('centres on the pads actually in use', () => {
    expect(fleetCenter(1)).toEqual({ x: 0, y: 0 });
    expect(fleetCenter(6)).toEqual({ x: 0, y: 5 });
    expect(fleetCenter(100)).toEqual({ x: 6, y: 14 });
  });
});

describe('solveAssignment', () => {
  const permutations = (n) => {
    if (n === 0) return [[]];
    return permutations(n - 1).flatMap((p) =>
      Array.from({ length: n }, (_, k) => [...p.slice(0, k), n - 1, ...p.slice(k)]));
  };

  it('finds the minimum-cost assignment', () => {
    // Deterministic pseudo-random costs, checked against brute force.
    let seed = 7;
    const rnd = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
    for (let trial = 0; trial < 20; trial++) {
      const n = 1 + (trial % 6);
      const cost = Array.from({ length: n }, () => Array.from({ length: n }, () => Math.round(rnd() * 100)));
      const total = (perm) => perm.reduce((s, c, r) => s + cost[r][c], 0);
      const best = Math.min(...permutations(n).map(total));
      const perm = solveAssignment(cost);
      expect(new Set(perm).size).toBe(n);
      expect(total(perm)).toBe(best);
    }
  });
});
