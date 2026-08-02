"""
Taramandal Studio - Collision-aware formation assignment.

When the fleet morphs from one formation to the next, WHICH drone flies to WHICH
target point matters enormously: a naive index-order mapping (drone i -> target i)
makes drones cross straight through each other. Solving the assignment problem
(minimum total squared travel) both shortens the morph and sharply reduces
crossings, because an optimal (non-crossing in the plane) matching almost never
sends two drones through the same region.
"""

from scipy.optimize import linear_sum_assignment
import numpy as np


def optimal_assignment(from_pts: list, to_pts: list) -> list:
    """
    Returns the minimum-total-squared-distance assignment between two equal-size
    formations. `from_pts` / `to_pts` are lists of {x, y, z}. The result is a list
    `perm` of length N where `perm[i]` is the index in `to_pts` that source point
    `i` is assigned to (a bijection — every target used exactly once).
    """
    n = len(from_pts)
    if n == 0:
        return []
    if len(to_pts) != n:
        raise ValueError(f"formations must be equal size (got {n} vs {len(to_pts)})")

    src = np.array([[p["x"], p["y"], p["z"]] for p in from_pts], dtype=float)
    dst = np.array([[p["x"], p["y"], p["z"]] for p in to_pts], dtype=float)
    # cost[i][j] = squared distance from source i to target j
    diff = src[:, None, :] - dst[None, :, :]
    cost = np.einsum("ijk,ijk->ij", diff, diff)

    row_ind, col_ind = linear_sum_assignment(cost)
    perm = [0] * n
    for r, c in zip(row_ind, col_ind):
        perm[int(r)] = int(c)
    return perm


def total_cost(from_pts: list, to_pts: list, perm: list) -> float:
    """Total squared travel for a given assignment (for tests / comparison)."""
    tot = 0.0
    for i, j in enumerate(perm):
        a, b = from_pts[i], to_pts[j]
        tot += (a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2 + (a["z"] - b["z"]) ** 2
    return tot
