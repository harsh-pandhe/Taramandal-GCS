"""Aerodynamically-aware formation assignment.

:func:`tara_core.assignment.optimal_assignment` minimises Cartesian travel and
nothing else. That is the right objective for morph *length*, but it is blind to
the one interaction that actually destroys drones mid-morph: rotor downwash.

A hovering multirotor throws a turbulent jet downward at >10 m/s. A drone that
crosses underneath another one flies into that jet, loses lift on the shadowed
side and drops out of the sky - and because the jet expands as it falls, "under"
is not a line but a cone. The industry figure for the half-angle of that cone is
30 degrees, which is what :data:`DOWNWASH_HALF_ANGLE_DEG` encodes.

The distance-optimal assignment has no reason to avoid this. Two drones swapping
across a formation take the shortest path whether or not one of those paths runs
directly beneath the other; the solver sees two straight lines that never
intersect in 3D and calls it safe. It is not safe: they never touch, and the
lower one still crashes.

This module adds an aerodynamic penalty term to the Hungarian cost matrix::

    C[i][j] = D(i, j) + lambda * sum over k != i of Phi(p_i(t), p_k(t))

where ``D`` is the initial Euclidean distance from drone ``i`` to target ``j``,
``Phi`` counts the discretized morph samples at which drone ``i`` sits inside
another drone's downwash cone, and ``lambda`` (:data:`DEFAULT_PENALTY_WEIGHT`,
1000.0) is large enough that avoiding the cone always dominates saving distance.
The penalty is a *cost*, not a constraint, so the solver still returns a complete
assignment when every option conflicts - it returns the least-bad one rather
than failing, and :class:`AssignmentReport` tells the caller what remains.

Coordinates are NED throughout: ``z`` is negative upwards, so ``z_i > z_k`` means
drone ``i`` is *below* drone ``k`` and therefore in its wash.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linear_sum_assignment

#: Half-angle of the expanding downwash cone beneath a rotorcraft, degrees.
DOWNWASH_HALF_ANGLE_DEG = 30.0

#: Penalty multiplier ``lambda``. One cone sample costs this much, which swamps
#: any plausible distance saving (arena diagonals are tens of metres, so ``D``
#: is at most a few hundred) and makes the solver treat cone avoidance as
#: effectively hard while still degrading gracefully if it is unavoidable.
DEFAULT_PENALTY_WEIGHT = 1000.0

#: Number of samples across the morph horizon, endpoints included. The morph is
#: a straight line per drone, so the cone test is sampled rather than solved in
#: closed form; 21 samples resolves a metre-scale intrusion over a typical
#: 10-30 m morph without making the penalty build expensive.
DEFAULT_MORPH_SAMPLES = 21

#: Vertical separation below which two drones are treated as level rather than
#: stacked, metres. Without this, floating-point noise on two nominally
#: co-planar drones makes whichever one is 1e-16 m lower "inside the cone" of
#: the other, since the cone radius at that depth is ~0 but so is their
#: horizontal separation when their paths cross. Real wash needs real vertical
#: offset to develop.
MIN_VERTICAL_OFFSET_M = 0.05


def _as_array(pts) -> np.ndarray:
    """Convert a list of ``{x, y, z}`` dicts (or ``(x, y, z)`` triples) to ``(n, 3)``."""
    out = np.empty((len(pts), 3), dtype=float)
    for i, p in enumerate(pts):
        if isinstance(p, dict):
            out[i] = (p["x"], p["y"], p["z"])
        else:
            out[i] = (p[0], p[1], p[2])
    return out


def in_downwash_cone(
    lower,
    upper,
    half_angle_deg: float = DOWNWASH_HALF_ANGLE_DEG,
) -> bool:
    """Is ``lower`` inside the downwash cone descending from ``upper``?

    Implements ``Phi`` for a single pair. In NED, ``lower`` is genuinely below
    ``upper`` when ``lower_z > upper_z`` (z negative up). The cone's radius at
    that depth is ``(z_lower - z_upper) * tan(half_angle)``, and the pair is a
    violation when their horizontal separation falls within it.

    Args:
        lower: Candidate victim ``(x, y, z)`` or ``{x, y, z}``, NED metres.
        upper: Candidate wash source, same shape.
        half_angle_deg: Cone half-angle. Defaults to 30 degrees.

    Returns:
        ``True`` if ``lower`` is in ``upper``'s wash.
    """
    lo = _as_array([lower])[0]
    up = _as_array([upper])[0]
    dz = lo[2] - up[2]  # NED: positive means `lower` really is lower
    if dz <= MIN_VERTICAL_OFFSET_M:
        return False
    radius = dz * math.tan(math.radians(half_angle_deg))
    horiz = math.hypot(lo[0] - up[0], lo[1] - up[1])
    return bool(horiz <= radius)


def _column_hits(
    src: np.ndarray,
    target: np.ndarray,
    ref_dst: np.ndarray,
    samples: int,
    half_angle_deg: float,
) -> np.ndarray:
    """Cone-intrusion counts for one column ``j`` of the cost matrix.

    Every drone ``i`` is flown from ``src[i]`` to the single point ``target``
    (candidate target ``j``) while the rest of the fleet flies its reference
    morph ``src[k] -> ref_dst[k]``. Returns the length-``n`` column of counts,
    i.e. ``sum over k != i of Phi`` accumulated across the morph horizon.

    Both directions are counted: an assignment that puts ``i`` above ``k`` is
    just as fatal as one that puts it below - the crash simply happens to the
    other drone - and only ``i``'s own row varies with ``j``, so the solver can
    act on the interaction only if it is charged to ``i``.
    """
    n = len(src)
    tan_half = math.tan(math.radians(half_angle_deg))
    col = np.zeros(n, dtype=float)

    for t in np.linspace(0.0, 1.0, samples):
        cand = src + t * (target - src)   # (n, 3): every i heading to `target`
        ref = src + t * (ref_dst - src)   # (n, 3): every k on its reference path

        dz = cand[:, 2][:, None] - ref[:, 2][None, :]
        horiz = np.hypot(
            cand[:, 0][:, None] - ref[:, 0][None, :],
            cand[:, 1][:, None] - ref[:, 1][None, :],
        )
        below = (dz > MIN_VERTICAL_OFFSET_M) & (horiz <= dz * tan_half)
        above = (-dz > MIN_VERTICAL_OFFSET_M) & (horiz <= -dz * tan_half)
        conflict = below | above
        np.fill_diagonal(conflict, False)  # a drone never washes itself
        col += conflict.sum(axis=1)

    return col


def _hits_matrix(
    src: np.ndarray,
    dst: np.ndarray,
    ref_dst: np.ndarray,
    samples: int,
    half_angle_deg: float,
) -> np.ndarray:
    """Assemble the full ``(n, n)`` cone-intrusion matrix, one column at a time.

    The other drones ``k`` fly ``src[k] -> ref_dst[k]``. That is deliberately an
    approximation: the true penalty depends on the whole permutation, which is
    exactly the thing being solved for, so an exact formulation is no longer a
    linear assignment problem at all. The reference morph makes the cost matrix
    well-defined and separable, and :func:`aerodynamic_assignment` closes the
    gap by re-solving against the assignment it just produced.
    """
    n = len(src)
    hits = np.zeros((n, n), dtype=float)
    if n < 2:
        return hits
    for j in range(n):
        hits[:, j] = _column_hits(src, dst[j], ref_dst, samples, half_angle_deg)
    return hits


def aerodynamic_cost_matrix(
    from_pts: list,
    to_pts: list,
    penalty_weight: float = DEFAULT_PENALTY_WEIGHT,
    samples: int = DEFAULT_MORPH_SAMPLES,
    half_angle_deg: float = DOWNWASH_HALF_ANGLE_DEG,
) -> np.ndarray:
    """Build the modified Hungarian cost matrix ``C[i][j]``.

    ``C[i][j] = D(i, j) + penalty_weight * Phi_ij``, with ``D`` the Euclidean
    initial distance and ``Phi_ij`` the cone-intrusion count for drone ``i``
    flying to target ``j`` against the identity reference morph.

    Euclidean distance is used rather than the squared distance
    :mod:`tara_core.assignment` minimises, so ``D`` and the penalty stay on
    comparable scales and one ``penalty_weight`` behaves the same way in a small
    arena as in a large one. It also matches the specified
    ``D(i, j) = |p_i(0) - q_j|_2``.

    Args:
        from_pts: Current formation, ``[{x, y, z}, ...]``.
        to_pts: Target formation, same length.
        penalty_weight: ``lambda``.
        samples: Morph-horizon discretization.
        half_angle_deg: Cone half-angle.

    Returns:
        The ``(n, n)`` cost matrix.

    Raises:
        ValueError: If the formations differ in size.
    """
    n = len(from_pts)
    if len(to_pts) != n:
        raise ValueError(f"formations must be equal size (got {n} vs {len(to_pts)})")
    if n == 0:
        return np.zeros((0, 0), dtype=float)

    src = _as_array(from_pts)
    dst = _as_array(to_pts)

    diff = src[:, None, :] - dst[None, :, :]
    distance = np.sqrt(np.einsum("ijk,ijk->ij", diff, diff))
    hits = _hits_matrix(src, dst, dst, samples, half_angle_deg)
    return distance + penalty_weight * hits


@dataclass
class AssignmentReport:
    """Outcome of an aerodynamic assignment.

    Attributes:
        perm: ``perm[i]`` is the target index assigned to source ``i``.
        conflicts: Remaining cone intrusions under ``perm``. Empty means the
            morph is downwash-clean.
        total_distance_m: Sum of Euclidean travel under ``perm``.
        iterations: Refinement passes actually run.
    """

    perm: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    total_distance_m: float = 0.0
    iterations: int = 0

    @property
    def is_clean(self) -> bool:
        """True when no drone enters another's downwash cone during the morph."""
        return not self.conflicts


def find_cone_conflicts(
    from_pts: list,
    to_pts: list,
    perm: list,
    samples: int = DEFAULT_MORPH_SAMPLES,
    half_angle_deg: float = DOWNWASH_HALF_ANGLE_DEG,
) -> list:
    """List every downwash-cone intrusion occurring under a given assignment.

    Unlike the cost-matrix build, this evaluates the *actual* permutation: every
    drone flies its assigned path and every ordered pair is checked at every
    sample. This is the ground truth used by the tests and by
    :func:`aerodynamic_assignment`'s convergence check.

    Args:
        from_pts: Current formation.
        to_pts: Target formation.
        perm: ``perm[i]`` = target index for source ``i``.
        samples: Morph-horizon discretization.
        half_angle_deg: Cone half-angle.

    Returns:
        Conflict dicts with ``time_ratio``, ``lower``, ``upper``,
        ``horizontal_m``, ``cone_radius_m`` and ``vertical_m``, ordered by time.
    """
    src = _as_array(from_pts)
    dst = _as_array(to_pts)
    n = len(src)
    if n < 2:
        return []

    assigned = np.array([dst[perm[i]] for i in range(n)])
    tan_half = math.tan(math.radians(half_angle_deg))

    conflicts = []
    for t in np.linspace(0.0, 1.0, samples):
        pos = src + t * (assigned - src)
        for a in range(n):
            for b in range(n):
                if a == b:
                    continue
                dz = pos[a][2] - pos[b][2]  # a is below b when positive (NED)
                if dz <= MIN_VERTICAL_OFFSET_M:
                    continue
                radius = dz * tan_half
                horiz = math.hypot(pos[a][0] - pos[b][0], pos[a][1] - pos[b][1])
                if horiz <= radius:
                    conflicts.append({
                        "time_ratio": round(float(t), 4),
                        "lower": a,
                        "upper": b,
                        "horizontal_m": round(float(horiz), 3),
                        "cone_radius_m": round(float(radius), 3),
                        "vertical_m": round(float(dz), 3),
                    })
    return conflicts


def _repair_by_swaps(
    from_pts: list,
    to_pts: list,
    perm: list,
    distance: np.ndarray,
    samples: int,
    half_angle_deg: float,
    max_passes: int = 6,
) -> tuple[list, list]:
    """Greedily swap target pairs while that genuinely reduces cone conflicts.

    The Hungarian pass minimises an *approximation* of the penalty (see
    :func:`_hits_matrix`), and that approximation can be badly wrong precisely
    when it matters: if the solve moves several drones off the reference morph
    at once, the fleet configuration it costed never actually occurs, so it can
    return an assignment it believes is conflict-free while the true count is
    high. Re-solving from the new reference just oscillates between two such
    assignments, each predicting zero hits and neither delivering it.

    This pass is the correction. It evaluates candidate swaps against
    :func:`find_cone_conflicts` — the real objective on the real paths, with no
    reference-morph approximation anywhere — and keeps a swap only when the true
    conflict count drops. Ties are broken toward the shorter total travel, so a
    swap that fixes nothing but lengthens the morph is never taken.

    Args:
        from_pts: Current formation.
        to_pts: Target formation.
        perm: Starting assignment, from the Hungarian solve.
        distance: Precomputed ``(n, n)`` Euclidean distance matrix.
        samples: Morph-horizon discretization.
        half_angle_deg: Cone half-angle.
        max_passes: Cap on full sweeps over all pairs.

    Returns:
        ``(perm, conflicts)`` — the improved assignment and its true conflicts.
    """
    n = len(perm)
    perm = list(perm)
    conflicts = find_cone_conflicts(from_pts, to_pts, perm, samples, half_angle_deg)

    def travel(p):
        return float(sum(distance[i][p[i]] for i in range(n)))

    for _ in range(max_passes):
        if not conflicts:
            break
        improved = False

        # Only drones actually implicated in a conflict are worth moving; every
        # other swap can at best shuffle a clean pairing into a longer one.
        involved = sorted({c["lower"] for c in conflicts} | {c["upper"] for c in conflicts})

        for a in involved:
            for b in range(n):
                if a == b:
                    continue
                candidate = list(perm)
                candidate[a], candidate[b] = candidate[b], candidate[a]
                cand_conflicts = find_cone_conflicts(
                    from_pts, to_pts, candidate, samples, half_angle_deg
                )
                better = len(cand_conflicts) < len(conflicts) or (
                    len(cand_conflicts) == len(conflicts)
                    and travel(candidate) < travel(perm) - 1e-9
                )
                if better:
                    perm, conflicts = candidate, cand_conflicts
                    improved = True
                    break
            if improved:
                break

        if not improved:
            break  # local optimum under pairwise swaps

    return perm, conflicts


def aerodynamic_assignment(
    from_pts: list,
    to_pts: list,
    penalty_weight: float = DEFAULT_PENALTY_WEIGHT,
    samples: int = DEFAULT_MORPH_SAMPLES,
    half_angle_deg: float = DOWNWASH_HALF_ANGLE_DEG,
    max_iterations: int = 4,
) -> AssignmentReport:
    """Solve the assignment with the aerodynamic penalty applied.

    Two stages, because neither alone is sufficient:

    1. **Hungarian solve.** The penalised cost matrix is built against a
       reference morph and solved exactly. This is globally optimal for the
       approximated cost and does the heavy lifting — it lands on or near a
       clean assignment in the common cases.
    2. **Swap repair.** :func:`_repair_by_swaps` then checks the result against
       the true conflict count and fixes what the approximation got wrong. This
       is what makes the function trustworthy: stage 1 alone can return an
       assignment it *believes* is clean while it is not, and no amount of
       re-solving fixes that (the iteration simply oscillates between two such
       assignments — see :func:`_repair_by_swaps` for the mechanism).

    Stage 1 is repeated up to ``max_iterations`` times with the reference morph
    updated to the last solution, and the best true result across every stage-1
    and stage-2 candidate is what gets returned — never simply the last one, so
    an iteration that makes things worse cannot degrade the outcome.

    Args:
        from_pts: Current formation, ``[{x, y, z}, ...]``.
        to_pts: Target formation, same length.
        penalty_weight: ``lambda``.
        samples: Morph-horizon discretization.
        half_angle_deg: Cone half-angle.
        max_iterations: Cap on Hungarian refinement passes.

    Returns:
        An :class:`AssignmentReport`. ``conflicts`` being non-empty means no
        conflict-free assignment was found, which for a starting formation that
        already has a drone inside a cone is not a solver failure but a property
        of the formation itself.

    Raises:
        ValueError: If the formations differ in size.
    """
    n = len(from_pts)
    if len(to_pts) != n:
        raise ValueError(f"formations must be equal size (got {n} vs {len(to_pts)})")
    if n == 0:
        return AssignmentReport(perm=[], conflicts=[], total_distance_m=0.0, iterations=0)

    src = _as_array(from_pts)
    dst = _as_array(to_pts)
    diff = src[:, None, :] - dst[None, :, :]
    distance = np.sqrt(np.einsum("ijk,ijk->ij", diff, diff))

    # `reference[k]` is the target drone k is assumed to fly to when it plays
    # the role of "other drone". Starts as the identity morph, then becomes each
    # solved assignment in turn.
    reference = list(range(n))
    best_perm = None
    best_conflicts = None
    iterations = 0
    seen = set()

    for _ in range(max_iterations):
        iterations += 1
        ref_dst = np.array([dst[reference[k]] for k in range(n)])
        hits = _hits_matrix(src, dst, ref_dst, samples, half_angle_deg)

        cost = distance + penalty_weight * hits
        row_ind, col_ind = linear_sum_assignment(cost)
        perm = [0] * n
        for r, c in zip(row_ind, col_ind):
            perm[int(r)] = int(c)

        # Ground the approximate solve in the true objective before judging it.
        perm, conflicts = _repair_by_swaps(
            from_pts, to_pts, perm, distance, samples, half_angle_deg
        )

        if (
            best_conflicts is None
            or len(conflicts) < len(best_conflicts)
            or (
                len(conflicts) == len(best_conflicts)
                and sum(distance[i][perm[i]] for i in range(n))
                < sum(distance[i][best_perm[i]] for i in range(n)) - 1e-9
            )
        ):
            best_perm, best_conflicts = perm, conflicts

        if not best_conflicts:
            break
        key = tuple(perm)
        if key in seen:
            break  # cycling between reference morphs; more passes cannot help
        seen.add(key)
        reference = perm

    total = float(sum(distance[i][best_perm[i]] for i in range(n)))
    return AssignmentReport(
        perm=best_perm,
        conflicts=best_conflicts,
        total_distance_m=total,
        iterations=iterations,
    )
