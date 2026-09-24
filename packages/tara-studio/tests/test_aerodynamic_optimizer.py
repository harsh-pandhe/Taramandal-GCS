"""Tests for the 30-degree downwash cone penalty and the dynamic-feasibility checks.

Geometry conventions used throughout: coordinates are NED metres, so ``z`` is
**negative upwards** — a drone hovering 10 m up is at ``z = -10.0``, and drone A
is below drone B exactly when ``z_A > z_B``. Getting that backwards silently
inverts every cone test, so the fixtures below state the intended altitude in a
comment wherever it is not obvious.
"""

import itertools
import math

import pytest

from tara_studio.optimizer import (
    DEFAULT_MORPH_SAMPLES,
    DEFAULT_PENALTY_WEIGHT,
    DOWNWASH_HALF_ANGLE_DEG,
    aerodynamic_assignment,
    aerodynamic_cost_matrix,
    find_cone_conflicts,
    in_downwash_cone,
)
from tara_studio.validator import (
    DEFAULT_DYNAMIC_LIMITS,
    DynamicLimits,
    validate_dynamics,
)
from tara_core.assignment import optimal_assignment

TAN30 = math.tan(math.radians(30.0))


def pts(coords):
    return [{"x": x, "y": y, "z": z} for (x, y, z) in coords]


def straight_line_show(tracks, rate=10.0):
    """Build a Studio-shaped show from ``{drone_id: [(x, y, z), ...]}`` samples."""
    return {
        "drones": [
            {
                "id": d_id,
                "waypoints": [
                    {"time": round(i / rate, 3), "x": x, "y": y, "z": z, "yaw": 0.0}
                    for i, (x, y, z) in enumerate(samples)
                ],
            }
            for d_id, samples in tracks.items()
        ]
    }


class TestDownwashConeGeometry:
    """The Phi predicate itself, before any solver is involved."""

    def test_directly_below_within_cone_is_flagged(self):
        # Drone at z=-10 (10 m up); victim 1.2 m below it at z=-8.8,
        # horizontally coincident. Unambiguously in the wash.
        assert in_downwash_cone((0.0, 0.0, -8.8), (0.0, 0.0, -10.0))

    def test_above_the_source_is_never_in_its_wash(self):
        # Same pair, roles reversed: wash goes down, not up.
        assert not in_downwash_cone((0.0, 0.0, -10.0), (0.0, 0.0, -8.8))

    def test_cone_radius_expands_at_thirty_degrees(self):
        # 1.2 m below => cone radius is 1.2*tan(30) = 0.6928 m.
        dz = 1.2
        radius = dz * TAN30
        just_inside = radius - 0.01
        just_outside = radius + 0.01
        assert in_downwash_cone((just_inside, 0.0, -8.8), (0.0, 0.0, -10.0))
        assert not in_downwash_cone((just_outside, 0.0, -8.8), (0.0, 0.0, -10.0))

    def test_level_drones_are_not_in_each_others_wash(self):
        # Identical altitude: no vertical offset, so no wash regardless of how
        # close they are horizontally (that is the separation check's job).
        assert not in_downwash_cone((0.0, 0.0, -10.0), (0.3, 0.0, -10.0))

    def test_far_below_needs_proportionally_more_horizontal_clearance(self):
        # 6 m below => radius 3.46 m, so a 3 m offset that was safe at 1.2 m
        # of separation is now inside the cone.
        assert not in_downwash_cone((3.0, 0.0, -8.8), (0.0, 0.0, -10.0))
        assert in_downwash_cone((3.0, 0.0, -4.0), (0.0, 0.0, -10.0))


class TestDownwashConeDetection:
    def test_downwash_cone_detection(self):
        """A drone passing directly under another at 1.2 m offset is flagged.

        Drone 0 crosses west-to-east along z=-8.8 (8.8 m up). Drone 1 hovers
        stationary at the origin at z=-10.0 (10 m up), i.e. 1.2 m above drone
        0's flight level. Midway through the morph drone 0 passes straight
        through the point directly beneath drone 1 — the textbook downwash
        kill.
        """
        from_pts = pts([(-6.0, 0.0, -8.8), (0.0, 0.0, -10.0)])
        to_pts = pts([(6.0, 0.0, -8.8), (0.0, 0.0, -10.0)])

        conflicts = find_cone_conflicts(from_pts, to_pts, perm=[0, 1])

        assert conflicts, "expected the under-pass to be flagged as a cone intrusion"
        # Drone 0 is the one in the wash; drone 1 is the source.
        assert any(c["lower"] == 0 and c["upper"] == 1 for c in conflicts)
        # 1.2 m of vertical offset, as constructed.
        assert all(c["vertical_m"] == pytest.approx(1.2, abs=0.01) for c in conflicts)
        # The intrusion happens around the midpoint of the morph.
        worst = min(conflicts, key=lambda c: c["horizontal_m"])
        assert worst["time_ratio"] == pytest.approx(0.5, abs=0.05)

    def test_a_clear_flyover_is_not_flagged(self):
        # Same crossing, but drone 1 is moved 5 m north. At 1.2 m of vertical
        # offset the cone radius is only 0.69 m, so 5 m of lateral clearance is
        # comfortably outside it.
        from_pts = pts([(-6.0, 0.0, -8.8), (0.0, 5.0, -10.0)])
        to_pts = pts([(6.0, 0.0, -8.8), (0.0, 5.0, -10.0)])
        assert find_cone_conflicts(from_pts, to_pts, perm=[0, 1]) == []

    def test_detection_is_symmetric_in_reported_roles(self):
        # Whichever drone is lower is reported as `lower`, independent of index
        # order: here drone 1 is the one underneath.
        from_pts = pts([(0.0, 0.0, -10.0), (-6.0, 0.0, -8.8)])
        to_pts = pts([(0.0, 0.0, -10.0), (6.0, 0.0, -8.8)])
        conflicts = find_cone_conflicts(from_pts, to_pts, perm=[0, 1])
        assert conflicts
        assert all(c["lower"] == 1 and c["upper"] == 0 for c in conflicts)


class TestHungarianPenaltyReassignment:
    def test_hungarian_penalty_reassignment(self):
        """The penalty reassigns targets so that zero drones enter the cone.

        Three drones sit in a line along y, all at x=0 and all laterally clear
        of one another, so the *starting* formation is downwash-clean and any
        conflict is caused purely by the assignment. Drones 0 and 2 are low
        (z=-5, i.e. 5 m up); drone 1 is high between them (z=-13).

        The targets are the same line 16 m east, but with the y positions of
        the low target and the high target exchanged. That makes the
        distance-optimal answer the identity: each drone flies almost straight
        east, which is shortest — and which drags drone 0 diagonally across to
        y=7 directly beneath drone 1's lane, holding it in the wash for 13 of
        the 21 morph samples.

        The clean answer swaps targets 0 and 1, so drone 0 stays low on the
        outside and drone 1 keeps the middle lane to itself. That costs 0.85 m
        of extra travel out of ~51 m, which is exactly the trade the penalty
        exists to make.
        """
        from_pts = pts([(0.0, 0.0, -5.0), (0.0, 7.0, -13.0), (0.0, 14.0, -5.0)])
        to_pts = pts([(16.0, 7.0, -5.0), (16.0, 0.0, -13.0), (16.0, 14.0, -5.0)])

        # The starting formation must itself be clean, or no assignment could
        # ever be — a drone already inside a cone at t=0 is a formation defect,
        # not an assignment one.
        assert find_cone_conflicts(from_pts, from_pts, [0, 1, 2]) == []

        # Baseline: the plain distance solver picks the unsafe identity...
        plain = optimal_assignment(from_pts, to_pts)
        assert plain == [0, 1, 2]
        assert find_cone_conflicts(from_pts, to_pts, plain), (
            "the distance-optimal assignment should be the unsafe one here, "
            "otherwise this test proves nothing"
        )

        # ...and the aerodynamic solver refuses it.
        report = aerodynamic_assignment(from_pts, to_pts)

        assert report.is_clean, f"cone not cleared: {report.conflicts}"
        assert find_cone_conflicts(from_pts, to_pts, report.perm) == []
        assert sorted(report.perm) == [0, 1, 2]  # still a bijection
        assert report.perm != plain  # it genuinely reassigned

    def test_penalty_buys_safety_for_a_small_distance_premium(self):
        # The same scenario, quantified: clearing the cone costs under a metre
        # of extra travel. If this ratio ever blows up, the penalty is being
        # applied to something other than the conflicting pairing.
        from_pts = pts([(0.0, 0.0, -5.0), (0.0, 7.0, -13.0), (0.0, 14.0, -5.0)])
        to_pts = pts([(16.0, 7.0, -5.0), (16.0, 0.0, -13.0), (16.0, 14.0, -5.0)])

        report = aerodynamic_assignment(from_pts, to_pts)
        plain = optimal_assignment(from_pts, to_pts)

        def travel(perm):
            return sum(
                math.dist(
                    (from_pts[i]["x"], from_pts[i]["y"], from_pts[i]["z"]),
                    (to_pts[perm[i]]["x"], to_pts[perm[i]]["y"], to_pts[perm[i]]["z"]),
                )
                for i in range(len(perm))
            )

        assert report.is_clean
        premium = travel(report.perm) - travel(plain)
        assert 0 < premium < 2.0, f"unexpected distance premium: {premium}"

    def test_penalty_dominates_the_distance_term_in_the_cost_matrix(self):
        # The whole point of lambda = 1000 is that one cone sample outweighs any
        # plausible distance saving. Verify directly on the matrix: the entry
        # for a conflicting pairing exceeds the entry for a longer-but-clean one.
        from_pts = pts([(0.0, 0.0, -6.0), (0.0, 0.0, -12.0)])
        to_pts = pts([(12.0, 0.0, -6.0), (12.0, 0.0, -12.0)])

        cost = aerodynamic_cost_matrix(from_pts, to_pts)
        # Drone 0 -> target 0 is the short, conflicted lane; -> target 1 is the
        # longer, clean climb.
        assert cost[0][0] > cost[0][1]
        assert cost[0][0] >= DEFAULT_PENALTY_WEIGHT

    def test_clean_morph_is_left_alone(self):
        # With no cone interaction anywhere, the penalty is zero everywhere and
        # the result must match the plain distance-optimal assignment.
        from_pts = pts([(0.0, 0.0, -10.0), (0.0, 20.0, -10.0)])
        to_pts = pts([(5.0, 0.0, -10.0), (5.0, 20.0, -10.0)])

        report = aerodynamic_assignment(from_pts, to_pts)
        assert report.is_clean
        assert report.perm == [0, 1]

    def test_four_drone_alternating_layers_are_cleared(self):
        # A denser case the three-drone version cannot cover: four drones in a
        # line, alternating low/high, whose targets shuffle the y positions so
        # the identity drags both low drones under high lanes (26 intrusions).
        # Three of the 24 permutations are clean; the solver must find one.
        from_pts = pts([(0.0, 0.0, -5.0), (0.0, 7.0, -13.0), (0.0, 14.0, -5.0), (0.0, 21.0, -13.0)])
        to_pts = pts([(18.0, 7.0, -5.0), (18.0, 0.0, -13.0), (18.0, 21.0, -5.0), (18.0, 14.0, -13.0)])

        assert find_cone_conflicts(from_pts, from_pts, [0, 1, 2, 3]) == []
        assert find_cone_conflicts(from_pts, to_pts, [0, 1, 2, 3]), "identity should be unsafe"

        report = aerodynamic_assignment(from_pts, to_pts)
        assert sorted(report.perm) == [0, 1, 2, 3]
        assert report.is_clean, f"cone not cleared: {report.conflicts}"

    def test_matches_exhaustive_search_on_a_small_case(self):
        # The only way to be sure the heuristic is not just plausible: brute
        # force all 24 permutations and confirm the solver's conflict count
        # equals the true optimum.
        from_pts = pts([(0.0, 0.0, -5.0), (0.0, 7.0, -13.0), (0.0, 14.0, -5.0), (0.0, 21.0, -13.0)])
        to_pts = pts([(18.0, 7.0, -5.0), (18.0, 0.0, -13.0), (18.0, 21.0, -5.0), (18.0, 14.0, -13.0)])

        best = min(
            len(find_cone_conflicts(from_pts, to_pts, list(p)))
            for p in itertools.permutations(range(4))
        )
        report = aerodynamic_assignment(from_pts, to_pts)
        assert len(report.conflicts) == best

    def test_reports_remaining_conflicts_rather_than_failing(self):
        # Three drones stacked over a single vertical column, staying put. Every
        # drone is inside the cone of the one above it before anything moves, so
        # no assignment can be clean -- this is a defect of the *formation*, not
        # of the assignment. The solver must still return a complete bijection
        # and report what remains, rather than raising or looping forever.
        from_pts = pts([(0.0, 0.0, -4.0), (0.0, 0.0, -6.0), (0.0, 0.0, -8.0)])
        to_pts = pts([(0.0, 0.0, -4.0), (0.0, 0.0, -6.0), (0.0, 0.0, -8.0)])

        report = aerodynamic_assignment(from_pts, to_pts)
        assert sorted(report.perm) == [0, 1, 2]
        assert not report.is_clean
        assert report.conflicts

    def test_size_mismatch_raises(self):
        with pytest.raises(ValueError):
            aerodynamic_assignment(pts([(0, 0, 0)]), pts([(0, 0, 0), (1, 1, 1)]))

    def test_empty_formation(self):
        report = aerodynamic_assignment([], [])
        assert report.perm == []
        assert report.is_clean

    def test_single_drone_has_nothing_to_conflict_with(self):
        report = aerodynamic_assignment(pts([(0.0, 0.0, -5.0)]), pts([(3.0, 0.0, -5.0)]))
        assert report.perm == [0]
        assert report.is_clean


class TestDynamicFeasibility:
    def test_max_velocity_and_accel_limits(self):
        """The validator rejects trajectories exceeding 4.0 m/s.

        A drone moving 0.6 m per 0.1 s sample is doing 6.0 m/s — over the 4.0
        m/s ceiling. The same track sampled at 0.3 m per step is 3.0 m/s and
        must pass, so the test pins the limit rather than just "rejects
        something".
        """
        fast = straight_line_show({0: [(i * 0.6, 0.0, -10.0) for i in range(20)]})
        report = validate_dynamics(fast)

        assert report["status"] == "FAILED"
        assert report["violations"]["velocity"]
        assert report["metrics"]["max_velocity_m_s"] == pytest.approx(6.0, abs=0.01)
        assert all(v["drone"] == 0 for v in report["violations"]["velocity"])

        slow = straight_line_show({0: [(i * 0.3, 0.0, -10.0) for i in range(20)]})
        ok = validate_dynamics(slow)
        assert ok["status"] == "PASSED"
        assert ok["metrics"]["max_velocity_m_s"] == pytest.approx(3.0, abs=0.01)

    def test_velocity_limit_boundary(self):
        # Just under 4.0 m/s is compliant; just over is not. The step is built by
        # rounding rather than accumulating `i * 0.4`, whose float error reaches
        # 4.0000000000000036 m/s and trips a strictly-greater-than check on a
        # track that is nominally exactly at the limit.
        under = straight_line_show({0: [(round(i * 0.39, 6), 0.0, -10.0) for i in range(15)]})
        assert not validate_dynamics(under)["violations"]["velocity"]

        over = straight_line_show({0: [(round(i * 0.41, 6), 0.0, -10.0) for i in range(15)]})
        assert validate_dynamics(over)["violations"]["velocity"]

    def test_centripetal_acceleration_limit(self):
        # A circle of radius r flown at speed v has kappa = 1/r and centripetal
        # acceleration v^2/r exactly. Pick r = 2 m, v = 3 m/s => 4.5 m/s^2,
        # comfortably over the 2.5 m/s^2 ceiling while the speed itself stays
        # legal, so this can only trip the centripetal check.
        radius, speed, dt = 2.0, 3.0, 0.1
        omega = speed / radius
        track = [
            (radius * math.cos(omega * i * dt), radius * math.sin(omega * i * dt), -10.0)
            for i in range(40)
        ]
        report = validate_dynamics(straight_line_show({0: track}))

        assert report["status"] == "FAILED"
        assert report["violations"]["centripetal"]
        assert not report["violations"]["velocity"], "speed alone should be legal here"
        assert report["metrics"]["max_centripetal_m_s2"] == pytest.approx(4.5, rel=0.02)

    def test_gentle_turn_passes_the_centripetal_check(self):
        # Same speed, much wider radius: r = 8 m at 3 m/s is 1.125 m/s^2, inside
        # the limit.
        radius, speed, dt = 8.0, 3.0, 0.1
        omega = speed / radius
        track = [
            (radius * math.cos(omega * i * dt), radius * math.sin(omega * i * dt), -10.0)
            for i in range(40)
        ]
        report = validate_dynamics(straight_line_show({0: track}))
        assert report["status"] == "PASSED"
        assert report["metrics"]["max_centripetal_m_s2"] == pytest.approx(1.125, rel=0.02)

    def test_straight_line_has_no_centripetal_acceleration(self):
        # Curvature of a straight path is zero no matter how fast it is flown --
        # this is what separates the centripetal check from total acceleration.
        track = straight_line_show({0: [(i * 0.3, 0.0, -10.0) for i in range(30)]})
        report = validate_dynamics(track)
        assert report["metrics"]["max_centripetal_m_s2"] == pytest.approx(0.0, abs=1e-6)

    def test_minimum_spatial_distance(self):
        # Two drones converging to 0.8 m apart, under the 1.5 m floor.
        show = straight_line_show({
            0: [(0.0, 0.0, -10.0)] * 10,
            1: [(0.0, 0.8, -10.0)] * 10,
        })
        report = validate_dynamics(show)

        assert report["status"] == "FAILED"
        assert report["violations"]["separation"]
        assert report["metrics"]["min_distance_m"] == pytest.approx(0.8, abs=0.01)

    def test_adequate_separation_passes(self):
        show = straight_line_show({
            0: [(0.0, 0.0, -10.0)] * 10,
            1: [(0.0, 3.0, -10.0)] * 10,
        })
        report = validate_dynamics(show)
        assert report["status"] == "PASSED"
        assert report["metrics"]["min_distance_m"] == pytest.approx(3.0, abs=0.01)

    def test_limits_are_the_values_the_issue_specifies(self):
        assert DEFAULT_DYNAMIC_LIMITS.max_velocity_ms == 4.0
        assert DEFAULT_DYNAMIC_LIMITS.max_centripetal_ms2 == 2.5
        assert DEFAULT_DYNAMIC_LIMITS.min_separation_m == 1.5
        assert DOWNWASH_HALF_ANGLE_DEG == 30.0
        assert DEFAULT_PENALTY_WEIGHT == 1000.0

    def test_custom_limits_are_honoured(self):
        # 3.0 m/s track: legal by default, rejected under a 2 m/s ceiling.
        show = straight_line_show({0: [(i * 0.3, 0.0, -10.0) for i in range(15)]})
        assert validate_dynamics(show)["status"] == "PASSED"

        strict = DynamicLimits(max_velocity_ms=2.0)
        assert validate_dynamics(show, limits=strict)["status"] == "FAILED"

    def test_empty_show_is_reported_not_crashed(self):
        report = validate_dynamics({"drones": []})
        assert report["status"] == "FAILED"
        assert "No drones" in report["summary"]

    def test_report_shape_matches_the_other_validator(self):
        # Issue #60 added jerk detection: a "jerk" violations category, a
        # "max_jerk_m_s3" metric, and a top-level "feasibility_certified"
        # gate (True iff no violations across any category — see
        # certify_feasibility's docstring for why the *other* validator's
        # velocity/acceleration checks, not this one's own centripetal one,
        # are what combine with it for an export-time certification).
        show = straight_line_show({0: [(i * 0.1, 0.0, -10.0) for i in range(10)]})
        report = validate_dynamics(show)
        assert set(report) == {"status", "summary", "metrics", "violations", "feasibility_certified"}
        assert set(report["violations"]) == {"velocity", "centripetal", "jerk", "separation"}
        assert set(report["metrics"]) == {
            "max_velocity_m_s",
            "max_centripetal_m_s2",
            "max_jerk_m_s3",
            "min_distance_m",
        }


class TestOptimizerScale:
    def test_thirty_drone_morph_stays_tractable_and_bijective(self):
        # Two stacked rings trading altitude -- the shape that actually produces
        # cone conflicts at scale -- confirming the solver still returns a valid
        # bijection and reduces conflicts versus the plain distance assignment.
        n = 30
        from_pts, to_pts = [], []
        for i in range(n):
            angle = 2 * math.pi * i / n
            layer = -6.0 if i % 2 == 0 else -12.0
            from_pts.append({"x": 10 * math.cos(angle), "y": 10 * math.sin(angle), "z": layer})
            to_pts.append({"x": 10 * math.cos(angle), "y": 10 * math.sin(angle), "z": -18.0 - layer})

        report = aerodynamic_assignment(from_pts, to_pts, samples=11)
        assert sorted(report.perm) == list(range(n))

        plain = optimal_assignment(from_pts, to_pts)
        plain_conflicts = find_cone_conflicts(from_pts, to_pts, plain, samples=11)
        assert len(report.conflicts) <= len(plain_conflicts)

    def test_sample_count_affects_resolution_not_correctness(self):
        # A brief intrusion can be missed by a coarse sweep; a dense one must
        # still catch it. Both must return a valid bijection either way.
        from_pts = pts([(-6.0, 0.0, -8.8), (0.0, 0.0, -10.0)])
        to_pts = pts([(6.0, 0.0, -8.8), (0.0, 0.0, -10.0)])

        for samples in (5, DEFAULT_MORPH_SAMPLES, 51):
            conflicts = find_cone_conflicts(from_pts, to_pts, [0, 1], samples=samples)
            assert conflicts, f"under-pass missed at {samples} samples"
