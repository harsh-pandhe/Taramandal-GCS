"""Regression tests for the Studio/GCS safety-threshold split.

The bug these pin down: the Studio validator certified shows at 0.8 m while the
GCS aborted at 1.5 m, so a show could pass design review and then be rejected at
upload — or fly and trigger a mid-show RTL. Anything separated by between 0.8 m
and 1.5 m fell into that gap.
"""

import pytest

from tara_core.geometry import sweep_times

from tara_core import (
    DEFAULT_LIMITS,
    MAX_REPORTED,
    SafetyLimits,
    Show,
    collision_errors,
    report_subset,
    validate_show,
)


def two_drones_apart(gap_m: float, duration: float = 4.0) -> dict:
    """A show with two drones hovering a fixed distance apart on the Y axis."""
    times = [i * 0.5 for i in range(int(duration / 0.5) + 1)]
    return {
        "drones": [
            {
                "id": 0,
                "waypoints": [
                    {"time": t, "x": 0.0, "y": 0.0, "z": -10.0, "yaw": 0.0} for t in times
                ],
            },
            {
                "id": 1,
                "waypoints": [
                    {"time": t, "x": 0.0, "y": gap_m, "z": -10.0, "yaw": 0.0} for t in times
                ],
            },
        ]
    }


class TestThresholdOrdering:
    def test_design_limit_is_stricter_than_runtime_limit(self):
        # The entire point: certify with margin above the abort threshold.
        assert DEFAULT_LIMITS.design_separation_m >= DEFAULT_LIMITS.min_separation_m
        assert DEFAULT_LIMITS.separation_margin_m > 0

    def test_inverted_limits_are_rejected(self):
        # This configuration is what the system effectively had before: a design
        # threshold looser than the runtime one.
        with pytest.raises(ValueError, match="must be >= min_separation_m"):
            SafetyLimits(min_separation_m=1.5, design_separation_m=0.8)

    def test_non_positive_limits_are_rejected(self):
        with pytest.raises(ValueError, match="must be positive"):
            SafetyLimits(max_velocity_ms=0.0)


class TestTheGapThatCausedTheBug:
    @pytest.mark.parametrize("gap", [0.9, 1.0, 1.2, 1.44])
    def test_shows_in_the_old_gap_are_now_rejected_by_both(self, gap):
        """0.8 m < gap < 1.5 m used to pass Studio and fail the GCS."""
        show = two_drones_apart(gap)
        assert validate_show(show)["status"] == "FAILED"
        assert collision_errors(show), f"GCS gate should reject a {gap} m separation"

    def test_comfortably_separated_show_passes_both(self):
        show = two_drones_apart(5.0)
        assert validate_show(show)["status"] == "PASSED"
        assert collision_errors(show) == []


class TestParityInvariant:
    @pytest.mark.parametrize("gap", [0.5, 1.0, 1.49, 1.51, 2.0, 2.5, 10.0])
    def test_anything_studio_certifies_the_gcs_accepts(self, gap):
        """The invariant that makes the two halves agree.

        Design validation is strictly stricter, so passing it must imply
        clearing the upload gate. If this ever fails, the two thresholds have
        drifted apart again.
        """
        show = two_drones_apart(gap)
        if validate_show(show)["status"] == "PASSED":
            assert collision_errors(show) == []

    def test_report_shape_is_stable(self):
        """The web app reads these keys directly."""
        report = validate_show(two_drones_apart(5.0))
        assert set(report) == {"status", "summary", "metrics", "violations", "worst_violations", "method"}
        assert set(report["metrics"]) == {
            "min_distance_m",
            "max_velocity_m_s",
            "max_acceleration_m_s2",
            "max_yaw_rate_deg_s",
            "max_altitude_m",
            "min_altitude_m",
        }
        assert set(report["violations"]) == {
            "proximity",
            "velocity",
            "acceleration",
            "yaw_rate",
            "altitude",
            "ground",
        }
        assert set(report["worst_violations"]) == {
            "proximity",
            "velocity",
            "acceleration",
            "yaw_rate",
            "altitude",
            "ground",
        }

    def test_separation_exactly_at_the_limit_is_compliant(self):
        """A launch grid spaced at exactly design_separation_m must not be a violation.

        scipy's query_pairs is inclusive of its radius, so without a strict
        filter every neighbouring pad on the grid reports as too close.
        """
        show = two_drones_apart(DEFAULT_LIMITS.design_separation_m)
        assert validate_show(show)["violations"]["proximity"] == []
        assert collision_errors(two_drones_apart(DEFAULT_LIMITS.min_separation_m)) == []

    def test_min_distance_metric_is_accurate(self):
        report = validate_show(two_drones_apart(3.25))
        assert report["metrics"]["min_distance_m"] == pytest.approx(3.25, abs=0.01)


class TestWorstViolationReporting:
    """Fix C (issue #138): Report must expose worst violation even after >50 earlier violations."""

    def test_surfaces_worst_proximity_violation_after_many_earlier_violations(self):
        # 60 timesteps with a 1.91 m violation, followed by a severe 0.67 m violation at t=6.5
        dt = 0.1
        wps0 = []
        wps1 = []
        for i in range(60):
            t = round(i * dt, 2)
            wps0.append({"time": t, "x": 0.0, "y": 0.0, "z": -10.0, "yaw": 0.0})
            wps1.append({"time": t, "x": 0.0, "y": 1.91, "z": -10.0, "yaw": 0.0})

        # Add later severe breach at t=6.0 to 6.5: separation drops to 0.67 m
        wps0.append({"time": 6.5, "x": 0.0, "y": 0.0, "z": -10.0, "yaw": 0.0})
        wps1.append({"time": 6.5, "x": 0.0, "y": 0.67, "z": -10.0, "yaw": 0.0})

        show = {"drones": [{"id": 0, "waypoints": wps0}, {"id": 1, "waypoints": wps1}]}
        report = validate_show(show, dt=dt)

        # 1. Total violations list must be capped at 50
        assert len(report["violations"]["proximity"]) <= 50

        # 2. Global worst violation (0.67 m) must NOT be dropped by the cap
        distances = [v["distance"] for v in report["violations"]["proximity"]]
        assert 0.67 in distances, f"0.67m worst violation was dropped from list: {distances}"

        # 3. Dedicated worst_violations field must contain deterministic evidence
        worst = report["worst_violations"]["proximity"]
        assert worst is not None
        assert worst["distance"] == 0.67
        assert worst["time"] == 6.5
        assert set(worst["drones"]) == {0, 1}
        assert "positions" in worst
        assert "0" in worst["positions"] and "1" in worst["positions"]

        # 4. Global metrics must accurately reflect the worst breach
        assert report["metrics"]["min_distance_m"] == 0.67
        assert report["status"] == "FAILED"

    def test_surfaces_worst_velocity_violation_after_many_earlier_violations(self):
        # Many small velocity violations followed by a massive speed spike
        # limit is 6.0 m/s
        dt = 0.1
        wps = [{"time": 0.0, "x": 0.0, "y": 0.0, "z": -10.0, "yaw": 0.0}]
        # 60 steps at velocity 7.0 m/s (dx = 0.7m per 0.1s)
        x = 0.0
        for i in range(1, 61):
            t = round(i * dt, 2)
            x += 0.70
            wps.append({"time": t, "x": round(x, 2), "y": 0.0, "z": -10.0, "yaw": 0.0})

        # At step 61, jump 2.5 m in 0.1s -> velocity 25.0 m/s
        x += 2.5
        wps.append({"time": 6.1, "x": round(x, 2), "y": 0.0, "z": -10.0, "yaw": 0.0})

        show = {"drones": [{"id": 0, "waypoints": wps}]}
        report = validate_show(show, dt=dt)

        assert len(report["violations"]["velocity"]) <= 50
        velocities = [v["velocity"] for v in report["violations"]["velocity"]]
        assert 25.0 in velocities
        assert report["worst_violations"]["velocity"]["velocity"] == 25.0
        assert report["metrics"]["max_velocity_m_s"] == 25.0

    def test_truncation_does_not_affect_passed_shows(self):
        show = two_drones_apart(5.0)
        report = validate_show(show)
        assert report["status"] == "PASSED"
        assert all(report["worst_violations"][cat] is None for cat in report["worst_violations"])

    def test_every_violation_carries_the_limit_it_broke_including_an_override(self):
        report = validate_show(two_drones_apart(2.5), separation=3.0)
        assert report["status"] == "FAILED"
        assert {v["limit"] for v in report["violations"]["proximity"]} == {3.0}
        assert report["worst_violations"]["proximity"]["limit"] == 3.0

        slow = SafetyLimits(max_velocity_ms=4.0)
        report = validate_show(_one_drone_with_speeds([5.0]), limits=slow, dt=0.1)
        assert {v["limit"] for v in report["violations"]["velocity"]} == {4.0}

    def test_the_worst_is_ranked_on_exact_values_not_rounded_ones(self):
        # 0.674 m and 0.671 m both report as 0.67; only the second is the worst.
        report = validate_show(_pair_with_gaps([1.9] * 30 + [0.674] + [1.9] * 30 + [0.671]), dt=0.1)
        worst = report["worst_violations"]["proximity"]
        assert worst["distance"] == 0.67
        assert worst["time"] == pytest.approx(6.1)

    def test_an_equally_bad_later_violation_does_not_replace_the_earliest(self):
        report = validate_show(_pair_with_gaps([1.0] + [1.9] * 70 + [1.0]), dt=0.1)
        assert report["worst_violations"]["proximity"]["time"] == 0.0

    def test_a_truncated_report_keeps_the_most_severe_half_in_time_order(self):
        gaps = [1.9 - 0.01 * (i % 7) for i in range(120)]
        report = validate_show(_pair_with_gaps(gaps), dt=0.1)
        listed = report["violations"]["proximity"]
        assert len(listed) == 50
        times = [v["time"] for v in listed]
        assert times == sorted(times)
        assert min(v["distance"] for v in listed) == report["metrics"]["min_distance_m"]
        # The 17 closest moments (1.84 m) are all among the 25 most severe kept.
        assert sum(1 for v in listed if v["distance"] == 1.84) == 17

    def test_the_verdict_and_metrics_do_not_depend_on_the_cap(self):
        few = validate_show(_pair_with_gaps([1.5] * 10), dt=0.1)
        many = validate_show(_pair_with_gaps([1.5] * 200), dt=0.1)
        assert few["status"] == many["status"] == "FAILED"
        assert few["metrics"] == many["metrics"]
        assert len(many["violations"]["proximity"]) == 50


class TestAltitudeCeiling:
    """Issue #148: the backend enforced no altitude ceiling at all. Reproduced
    on origin/main before this fix: two static drones at z=-150 (150 m up)
    read PASSED, and the GCS uploader accepted the same show unchanged.
    """

    @pytest.mark.parametrize("altitude_m,expected_status", [
        (119.99, "PASSED"),
        (120.00, "PASSED"),  # strict: exactly at the limit must still pass
        (120.01, "FAILED"),
        (150.00, "FAILED"),
    ])
    def test_the_120m_boundary_is_strict_with_no_tolerance(self, altitude_m, expected_status):
        report = validate_show(_hovering_at(altitude_m))
        assert report["status"] == expected_status
        assert report["metrics"]["max_altitude_m"] == altitude_m

    def test_an_interpolated_segment_peak_between_sweep_samples_is_not_missed(self):
        # A waypoint peak of 125 m sits at t=0.15s, strictly between the dt=0.1
        # sweep's instants at 0.1s and 0.2s. The *sweep-interpolated* altitude
        # at both of those instants happens to land exactly on 120.0 m (passes
        # on its own) -- a check that only looked at sweep samples, rather
        # than at the waypoints themselves, would miss this violation
        # entirely. Altitude is linear between waypoints, so its true extremes
        # are always exactly at a waypoint; see _altitude_extremes.
        show = {"drones": [{"id": 0, "waypoints": [
            {"time": 0.0, "x": 0.0, "y": 0.0, "z": -110.0, "yaw": 0.0},
            {"time": 0.15, "x": 0.0, "y": 0.0, "z": -125.0, "yaw": 0.0},
            {"time": 0.3, "x": 0.0, "y": 0.0, "z": -110.0, "yaw": 0.0},
        ]}]}
        report = validate_show(show, dt=0.1)
        assert report["status"] == "FAILED"
        assert len(report["violations"]["altitude"]) == 1
        worst = report["worst_violations"]["altitude"]
        assert worst == {"time": 0.15, "drone": 0, "altitude": 125.0, "limit": 120.0}
        assert report["metrics"]["max_altitude_m"] == 125.0

    def test_worst_violation_and_metric_are_the_true_global_peak_not_a_sampled_one(self):
        show = {"drones": [{"id": 0, "waypoints": [
            {"time": 0.0, "x": 0.0, "y": 0.0, "z": -121.0, "yaw": 0.0},   # just over
            {"time": 10.0, "x": 0.0, "y": 0.0, "z": -180.0, "yaw": 0.0},  # the true worst
            {"time": 20.0, "x": 0.0, "y": 0.0, "z": -122.0, "yaw": 0.0},  # also over
        ]}]}
        report = validate_show(show)
        assert report["worst_violations"]["altitude"]["altitude"] == 180.0
        assert report["worst_violations"]["altitude"]["time"] == 10.0
        assert report["metrics"]["max_altitude_m"] == 180.0

    def test_does_not_fire_on_a_merely_distant_but_legal_altitude(self):
        report = validate_show(_hovering_at(30.0))
        assert report["status"] == "PASSED"
        assert report["violations"]["altitude"] == []
        assert report["worst_violations"]["altitude"] is None

    def test_does_not_affect_separation_velocity_or_yaw_rate_checks(self):
        # A 150 m-high show that is otherwise perfectly formed must still
        # report its other categories accurately -- the new check does not
        # short-circuit or mask the existing ones.
        show = {"drones": [
            {"id": 0, "waypoints": [{"time": 0.0, "x": 0.0, "y": 0.0, "z": -150.0, "yaw": 0.0}]},
            {"id": 1, "waypoints": [{"time": 0.0, "x": 10.0, "y": 0.0, "z": -150.0, "yaw": 0.0}]},
        ]}
        report = validate_show(show)
        assert report["violations"]["proximity"] == []
        assert report["violations"]["velocity"] == []
        assert report["violations"]["altitude"] != []


class TestGroundPenetration:
    """Tracked as its own category (not folded into 'altitude') per #148:
    a drone below ground and a drone over the ceiling are different failure
    modes with different causes, and conflating them would hide which one
    actually happened.
    """

    @pytest.mark.parametrize("z,expected_status", [
        (0.0, "PASSED"),       # exactly at ground level, strict, no tolerance
        (0.01, "FAILED"),      # NED: positive z is below ground
        (5.0, "FAILED"),
    ])
    def test_the_ground_boundary_is_strict(self, z, expected_status):
        show = {"drones": [{"id": 0, "waypoints": [{"time": 0.0, "x": 0.0, "y": 0.0, "z": z, "yaw": 0.0}]}]}
        report = validate_show(show)
        assert report["status"] == expected_status
        if expected_status == "FAILED":
            assert report["violations"]["ground"][0]["altitude"] == pytest.approx(-z)
            assert report["violations"]["altitude"] == []  # not double-counted as a ceiling breach

    def test_min_altitude_metric_reflects_the_true_trough(self):
        show = {"drones": [{"id": 0, "waypoints": [
            {"time": 0.0, "x": 0.0, "y": 0.0, "z": 3.0, "yaw": 0.0},
            {"time": 5.0, "x": 0.0, "y": 0.0, "z": -10.0, "yaw": 0.0},
        ]}]}
        report = validate_show(show)
        assert report["metrics"]["min_altitude_m"] == -3.0


def _hovering_at(altitude_m: float, duration: float = 4.0) -> dict:
    """One drone holding a fixed altitude (NED: z = -altitude_m)."""
    times = [i * 0.5 for i in range(int(duration / 0.5) + 1)]
    return {
        "drones": [{"id": 0, "waypoints": [
            {"time": t, "x": 0.0, "y": 0.0, "z": -altitude_m, "yaw": 0.0} for t in times
        ]}]
    }


def _pair_with_gaps(gaps: list[float], dt: float = 0.1) -> dict:
    """Two drones whose Y gap is ``gaps[k]`` at time ``k * dt``."""
    times = [round(k * dt, 2) for k in range(len(gaps))]
    return {
        "drones": [
            {"id": 0, "waypoints": [{"time": t, "x": 0.0, "y": 0.0, "z": -10.0, "yaw": 0.0} for t in times]},
            {"id": 1, "waypoints": [
                {"time": t, "x": 0.0, "y": g, "z": -10.0, "yaw": 0.0} for t, g in zip(times, gaps)
            ]},
        ]
    }


def _one_drone_with_speeds(speeds: list[float], dt: float = 0.1) -> dict:
    """One drone flying along X at ``speeds[k]`` m/s during step ``k``."""
    x, wps = 0.0, [{"time": 0.0, "x": 0.0, "y": 0.0, "z": -10.0, "yaw": 0.0}]
    for k, v in enumerate(speeds, start=1):
        x += v * dt
        wps.append({"time": round(k * dt, 2), "x": x, "y": 0.0, "z": -10.0, "yaw": 0.0})
    return {"drones": [{"id": 0, "waypoints": wps}]}



class TestShowShapeInterop:
    def test_studio_and_gcs_shapes_validate_identically(self):
        studio_shape = two_drones_apart(1.0)
        gcs_shape = Show.from_any(studio_shape).to_gcs()
        assert validate_show(studio_shape) == validate_show(gcs_shape)

    def test_round_trip_preserves_waypoints(self):
        original = Show.from_any(two_drones_apart(2.5))
        assert Show.from_any(original.to_gcs()).drones == original.drones
        assert Show.from_any(original.to_studio()).drones == original.drones

    def test_missing_required_key_is_rejected(self):
        with pytest.raises(ValueError, match="missing"):
            Show.from_any({"drones": [{"id": 0, "waypoints": [{"time": 0.0, "x": 0.0}]}]})

    def test_empty_show_fails_rather_than_silently_passing(self):
        assert validate_show({"drones": []})["status"] == "FAILED"


def _found(severities: list[float]) -> list[tuple[float, dict]]:
    """Violations in time order, entry ``i`` at time ``i`` with the given severity."""
    return [(severity, {"time": i}) for i, severity in enumerate(severities)]


class TestReportSubset:
    def test_a_short_list_comes_back_whole_and_in_order(self):
        found = _found([3.0, 1.0, 2.0])
        assert report_subset(found) == [entry for _, entry in found]

    def test_the_default_cap_is_max_reported(self):
        assert len(report_subset(_found([1.0] * 500))) == MAX_REPORTED == 50

    @pytest.mark.parametrize("cap", [4, 10, 50, 200])
    def test_never_returns_more_than_the_cap(self, cap):
        assert len(report_subset(_found([float(i % 7) for i in range(1000)]), cap=cap)) == cap

    @pytest.mark.parametrize("cap", [10, 50, 200])
    def test_a_list_within_the_cap_is_not_truncated(self, cap):
        assert len(report_subset(_found([1.0] * cap), cap=cap)) == cap

    def test_the_most_severe_half_of_the_cap_is_always_kept(self):
        # Severity rises with time, so the worst are the last: first-N would drop them all.
        found = _found([float(i) for i in range(300)])
        times = [e["time"] for e in report_subset(found, cap=10)]
        assert {295, 296, 297, 298, 299} <= set(times)
        assert len(times) == 10

    def test_the_global_worst_is_kept_wherever_it_falls(self):
        for worst_at in (0, 137, 299):
            severities = [1.0] * 300
            severities[worst_at] = 9.0
            assert worst_at in [e["time"] for e in report_subset(_found(severities), cap=10)]

    def test_output_is_in_time_order(self):
        severities = [float((i * 37) % 101) for i in range(400)]
        times = [e["time"] for e in report_subset(_found(severities), cap=20)]
        assert times == sorted(times)

    def test_the_rest_sample_the_whole_timeline(self):
        times = [e["time"] for e in report_subset(_found([1.0] * 1000), cap=10)]
        assert max(times) > 500, "equal severities must still reach the late timeline"

    def test_equally_severe_ties_go_to_the_earliest(self):
        # Three tie for the top two slots of a cap of 4: the earliest two win.
        severities = [1.0] * 100
        for i in (10, 50, 90):
            severities[i] = 5.0
        times = [e["time"] for e in report_subset(_found(severities), cap=4)]
        assert 10 in times and 50 in times
        assert 90 not in times

    def test_a_non_empty_list_never_comes_back_empty(self):
        assert report_subset(_found([1.0] * 100), cap=1)
        assert report_subset(_found([1.0] * 100), cap=2)

    def test_an_empty_list_stays_empty(self):
        assert report_subset([]) == []


class TestReportMethod:
    """Issue #126: a report states how its numbers were measured."""

    def test_report_states_sampled_separation_and_the_sweep_interval(self):
        report = validate_show(two_drones_apart(5.0))
        method = report["method"]
        assert method["sweep_dt_s"] == 0.1
        assert method["min_distance"] == "sampled"
        assert method["motion"] == "backward_difference"
        assert method["altitude"] == "waypoint_exact"
        # two_drones_apart runs 4 s; 0.1 s sampling covers 0.0 through 4.0
        # inclusive, 41 samples.
        assert method["samples"] == len(sweep_times(4.0, 0.1)) == 41

    def test_the_stated_interval_follows_the_dt_actually_used(self):
        coarse = validate_show(two_drones_apart(5.0), dt=0.5)["method"]
        fine = validate_show(two_drones_apart(5.0), dt=0.05)["method"]
        assert coarse["sweep_dt_s"] == 0.5 and fine["sweep_dt_s"] == 0.05
        assert fine["samples"] > coarse["samples"]

    def test_an_empty_show_still_carries_the_method(self):
        report = validate_show({"drones": []})
        assert report["status"] == "FAILED"
        assert report["method"]["samples"] == 0
        assert report["method"]["min_distance"] == "sampled"

