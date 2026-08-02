"""Tests for /api/assign-chain — calls the route function directly rather than
through an HTTP client (no test-only httpx dependency needed for this), which
is sufficient since the interesting logic is the chain computation itself,
not FastAPI's request handling.
"""

import pytest
from fastapi import HTTPException

from tara_studio.server import AssignChainRequest, api_assign_chain


def pts(coords):
    return [{"x": x, "y": y, "z": z} for (x, y, z) in coords]


class TestAssignChain:
    def test_first_formation_is_always_identity(self):
        forms = [pts([(0, 0, 0), (1, 0, 0), (2, 0, 0)])]
        result = api_assign_chain(AssignChainRequest(forms=forms))
        assert result["target_idx"][0] == [0, 1, 2]

    def test_single_formation_needs_no_assignment_calls(self):
        forms = [pts([(0, 0, 0), (1, 0, 0)])]
        result = api_assign_chain(AssignChainRequest(forms=forms))
        assert len(result["target_idx"]) == 1

    def test_resolves_a_swap_between_the_first_two_formations(self):
        # a and b are the same two points with labels swapped, so the optimal
        # assignment sends each drone to the target sharing its own physical
        # position (zero-cost) rather than the identity, which would move
        # both drones through each other (cost 200 each way).
        a = pts([(0, 0, 0), (10, 0, 0)])
        b = pts([(10, 0, 0), (0, 0, 0)])
        result = api_assign_chain(AssignChainRequest(forms=[a, b]))
        assert result["target_idx"] == [[0, 1], [1, 0]]

    def test_chain_is_consistent_across_more_than_two_formations(self):
        # Not a hand-derived expectation (fragile for anything past a trivial
        # swap — see test_matches_chaining_the_single_pair_endpoint_by_hand
        # for the real cross-check); this just pins down structural
        # invariants: one permutation per formation, formation 0 is always
        # the identity, and every permutation is a genuine bijection.
        a = pts([(0, 0, 0), (10, 0, 0), (5, 8, 0)])
        b = pts([(1, 1, 0), (9, 1, 0), (5, 7, 0)])
        c = pts([(0, 0, 0), (5, 8, 0), (10, 0, 0)])
        result = api_assign_chain(AssignChainRequest(forms=[a, b, c]))
        target_idx = result["target_idx"]
        assert len(target_idx) == 3
        assert target_idx[0] == [0, 1, 2]
        for perm in target_idx:
            assert sorted(perm) == [0, 1, 2]

    def test_matches_chaining_the_single_pair_endpoint_by_hand(self):
        # The whole point of assign-chain is to do server-side what the
        # frontend used to do with N sequential /api/assign calls — confirm
        # they produce the same result for a nontrivial multi-formation chain.
        from tara_core.assignment import optimal_assignment

        forms = [
            pts([(0, 0, 0), (5, 0, 0), (10, 0, 0)]),
            pts([(0, 5, 0), (5, 5, 0), (10, 5, 0)]),
            pts([(0, 10, 0), (10, 10, 0), (5, 10, 0)]),
        ]
        expected = [[0, 1, 2]]
        for s in range(len(forms) - 1):
            current = expected[s]
            from_pts = [forms[s][current[i]] for i in range(len(forms[s]))]
            expected.append(optimal_assignment(from_pts, forms[s + 1]))

        result = api_assign_chain(AssignChainRequest(forms=forms))
        assert result["target_idx"] == expected

    def test_rejects_empty_forms_list(self):
        with pytest.raises(HTTPException):
            api_assign_chain(AssignChainRequest(forms=[]))

    def test_rejects_mismatched_formation_sizes(self):
        forms = [pts([(0, 0, 0), (1, 0, 0)]), pts([(0, 0, 0)])]
        with pytest.raises(HTTPException):
            api_assign_chain(AssignChainRequest(forms=forms))

    def test_scales_to_100_drones_across_several_formations(self):
        import math

        def ring(n, radius):
            return [
                {"x": radius * math.cos(2 * math.pi * i / n), "y": radius * math.sin(2 * math.pi * i / n), "z": 0.0}
                for i in range(n)
            ]

        forms = [ring(100, 10.0), ring(100, 15.0), ring(100, 8.0)]
        result = api_assign_chain(AssignChainRequest(forms=forms))
        assert len(result["target_idx"]) == 3
        for perm in result["target_idx"]:
            assert sorted(perm) == list(range(100))  # each is a real bijection
