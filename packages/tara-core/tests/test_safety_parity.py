"""Regression tests for the Studio/GCS safety-threshold split.

The bug these pin down: the Studio validator certified shows at 0.8 m while the
GCS aborted at 1.5 m, so a show could pass design review and then be rejected at
upload — or fly and trigger a mid-show RTL. Anything separated by between 0.8 m
and 1.5 m fell into that gap.
"""

import pytest

from tara_core import DEFAULT_LIMITS, SafetyLimits, Show, collision_errors, validate_show


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
        assert set(report) == {"status", "summary", "metrics", "violations", "worst_violations"}
        assert set(report["metrics"]) == {
            "min_distance_m",
            "max_velocity_m_s",
            "max_acceleration_m_s2",
            "max_yaw_rate_deg_s",
        }
        assert set(report["violations"]) == {
            "proximity",
            "velocity",
            "acceleration",
            "yaw_rate",
        }
        assert set(report["worst_violations"]) == {
            "proximity",
            "velocity",
            "acceleration",
            "yaw_rate",
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
