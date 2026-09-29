// Minimum-total-squared-distance assignment between two equal-size point sets —
// the same objective as tara_core.assignment.optimal_assignment. The sequencer's
// collision-free guarantee depends on it, so it exists client-side as the
// fallback when the backend's /api/assign-chain is unreachable, and so the
// engine can be tested without a server.

/**
 * Hungarian algorithm (shortest augmenting path with potentials), O(n^3).
 * @param {number[][]} cost  n x n
 * @returns {number[]} perm, perm[i] = column assigned to row i
 */
export const solveAssignment = (cost) => {
  const n = cost.length;
  const u = new Float64Array(n + 1), v = new Float64Array(n + 1);
  const p = new Int32Array(n + 1), way = new Int32Array(n + 1);
  for (let i = 1; i <= n; i++) {
    p[0] = i;
    let j0 = 0;
    const minv = new Float64Array(n + 1).fill(Infinity);
    const used = new Uint8Array(n + 1);
    do {
      used[j0] = 1;
      const i0 = p[j0];
      let delta = Infinity, j1 = 0;
      for (let j = 1; j <= n; j++) {
        if (used[j]) continue;
        const cur = cost[i0 - 1][j - 1] - u[i0] - v[j];
        if (cur < minv[j]) { minv[j] = cur; way[j] = j0; }
        if (minv[j] < delta) { delta = minv[j]; j1 = j; }
      }
      for (let j = 0; j <= n; j++) {
        if (used[j]) { u[p[j]] += delta; v[j] -= delta; } else minv[j] -= delta;
      }
      j0 = j1;
    } while (p[j0] !== 0);
    do { const j1 = way[j0]; p[j0] = p[j1]; j0 = j1; } while (j0);
  }
  const perm = new Array(n);
  for (let j = 1; j <= n; j++) perm[p[j] - 1] = j - 1;
  return perm;
};

/** perm[i] = index in `to` that point `from[i]` is sent to. */
export const optimalAssignment = (from, to) => solveAssignment(
  from.map((a) => to.map((b) => (a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)),
);
