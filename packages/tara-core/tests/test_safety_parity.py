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
        assert set(report) == {"status", "summary", "metrics", "violations"}
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
