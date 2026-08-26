"""Pairwise proximity checking, shared by the validator, the uploader and the
live fleet monitor.

There were two implementations before this: a ``cKDTree`` one in the Studio
validator and the live monitor, and a hand-rolled O(n^2) double loop in the GCS
uploader. At 100 drones the double loop evaluates ~4,950 pairs per sample and
~300 samples per show — about 1.5 M distance computations for a single upload.
The tree does the same work in O(n log n) per sample by only ever examining
pairs that are actually close.
"""

from __future__ import annotations

import numpy as np
from scipy.spatial import cKDTree


def close_pairs(
    positions: dict[int, tuple[float, float, float]], threshold: float
) -> list[tuple[int, int, float]]:
    """Find every pair of drones closer than ``threshold``.

    Args:
        positions: Mapping of drone id to an ``(x, y, z)`` tuple, metres.
        threshold: Separation below which a pair is reported, metres.

    Returns:
        ``(id_a, id_b, distance)`` tuples with ``id_a < id_b``, sorted closest
        first. Empty if fewer than two drones are present. A pair separated by
        *exactly* ``threshold`` is compliant and is not reported.
    """
    ids = list(positions.keys())
    if len(ids) < 2:
        return []

    coords = np.array([positions[i] for i in ids], dtype=float)
    tree = cKDTree(coords)

    found = []
    # query_pairs is inclusive of the radius, but separation equal to the limit
    # satisfies it — a launch grid spaced at exactly design_separation_m would
    # otherwise report every neighbouring pad as a violation. Filter strictly.
    for i, j in tree.query_pairs(threshold):
        dist = float(np.linalg.norm(coords[i] - coords[j]))
        if dist >= threshold:
            continue
        a, b = ids[i], ids[j]
        found.append((a, b, dist) if a < b else (b, a, dist))

    found.sort(key=lambda p: p[2])
    return found


def min_separation(
    positions: dict[int, tuple[float, float, float]]
) -> float:
    """Return the smallest distance between any two drones, metres.

    Returns ``inf`` when fewer than two drones are present, so callers can fold
    it into a running minimum without special-casing.
    """
    ids = list(positions.keys())
    if len(ids) < 2:
        return float("inf")

    coords = np.array([positions[i] for i in ids], dtype=float)
    # k=2 because the nearest neighbour of any point is itself at distance 0.
    dists, _ = cKDTree(coords).query(coords, k=2)
    return float(np.min(dists[:, 1]))
