import pytest
from studio.assignment import optimal_assignment, total_cost


def pts(coords):
    return [{"x": x, "y": y, "z": z} for (x, y, z) in coords]


def test_assignment_is_a_bijection():
    frm = pts([(0, 0, 0), (2, 0, 0), (4, 0, 0), (6, 0, 0)])
    to = pts([(0, 3, 0), (2, 3, 0), (4, 3, 0), (6, 3, 0)])
    perm = optimal_assignment(frm, to)
    assert sorted(perm) == [0, 1, 2, 3]  # every target used exactly once


def test_optimal_beats_or_equals_identity():
    frm = pts([(0, 0, 0), (10, 0, 0), (5, 8, 0), (5, -8, 0)])
    to = pts([(1, 1, 0), (9, 1, 0), (5, 7, 0), (5, -7, 0)])
    perm = optimal_assignment(frm, to)
    identity = list(range(len(frm)))
    assert total_cost(frm, to, perm) <= total_cost(frm, to, identity)


def test_resolves_a_straight_swap():
    # Two drones whose targets are each other's positions. Identity mapping would
    # send them straight through one another (cost 200); the optimal assignment
    # is the swap (cost 0).
    frm = pts([(0, 0, 0), (10, 0, 0)])
    to = pts([(10, 0, 0), (0, 0, 0)])
    perm = optimal_assignment(frm, to)
    assert perm == [1, 0]
    assert total_cost(frm, to, perm) == 0.0
    assert total_cost(frm, to, [0, 1]) == 200.0


def test_size_mismatch_raises():
    with pytest.raises(ValueError):
        optimal_assignment(pts([(0, 0, 0)]), pts([(0, 0, 0), (1, 1, 1)]))


def test_empty_formation():
    assert optimal_assignment([], []) == []
