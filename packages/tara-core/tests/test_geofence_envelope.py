"""Issue #53: dynamic geofence envelope.

``geofence_radius_m`` used to be a single hardcoded 30.0 m, checked as a hard
RTL boundary against every drone's live position regardless of what the show
actually flies. A circle at N=100 needs ~38 m of radius, so any show whose
formation carries a drone further from the origin than 30 m false-positive
aborted mid-flight.

These tests pin down the fix: ``Show.max_horizontal_radius_m()`` computes the
real envelope from every waypoint of every drone (entry-blend + show + exit,
not just launch pads), and ``SafetyLimits.geofence_radii()`` derives a
soft-warning and a hard-RTL radius from it with fixed buffers.
"""

from __future__ import annotations

import pytest

from tara_core import DEFAULT_LIMITS, SafetyLimits
from tara_core.show import Show


def _show(entries: dict[int, list[dict]]) -> Show:
    return Show(drones=entries)


class TestMaxHorizontalRadius:
    def test_empty_show_is_zero(self):
        assert Show().max_horizontal_radius_m() == 0.0

    def test_single_stationary_drone(self):
        show = _show({0: [{"time": 0.0, "x": 3.0, "y": 4.0, "z": -2.0}]})
        assert show.max_horizontal_radius_m() == pytest.approx(5.0)

    def test_uses_every_waypoint_not_just_t0(self):
        """A drone that starts near home but sweeps out mid-show must count.

        This is the exact bug scenario: a pad-only bounding box would miss a
        formation excursion that happens after takeoff.
        """
        show = _show(
            {
                0: [
                    {"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0},  # pad: origin
                    {"time": 5.0, "x": 40.0, "y": 0.0, "z": -2.0},  # mid-show: far out
                    {"time": 10.0, "x": 0.0, "y": 0.0, "z": -2.0},  # exit: back home
                ]
            }
        )
        assert show.max_horizontal_radius_m() == pytest.approx(40.0)

    def test_z_is_ignored_only_horizontal_distance_counts(self):
        show = _show({0: [{"time": 0.0, "x": 3.0, "y": 4.0, "z": -500.0}]})
        assert show.max_horizontal_radius_m() == pytest.approx(5.0)

    def test_max_across_multiple_drones(self):
        show = _show(
            {
                0: [{"time": 0.0, "x": 1.0, "y": 0.0, "z": -2.0}],
                1: [{"time": 0.0, "x": 20.0, "y": 0.0, "z": -2.0}],
                2: [{"time": 0.0, "x": 5.0, "y": 5.0, "z": -2.0}],
            }
        )
        assert show.max_horizontal_radius_m() == pytest.approx(20.0)


class TestGeofenceRadiiDerivation:
    def test_soft_and_hard_use_their_respective_buffers(self):
        limits = SafetyLimits()
        soft, hard = limits.geofence_radii(25.0)
        assert soft == pytest.approx(25.0 + limits.geofence_soft_warning_buffer_m)
        assert hard == pytest.approx(25.0 + limits.geofence_hard_buffer_m)

    def test_default_buffers_match_acceptance_criteria(self):
        """Soft warning at envelope+10m, hard RTL at envelope+15m."""
        limits = DEFAULT_LIMITS
        soft, hard = limits.geofence_radii(38.0)
        assert soft == pytest.approx(48.0)
        assert hard == pytest.approx(53.0)

    def test_hard_is_always_looser_than_soft(self):
        soft, hard = DEFAULT_LIMITS.geofence_radii(0.0)
        assert hard > soft

    def test_negative_envelope_is_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            DEFAULT_LIMITS.geofence_radii(-1.0)

    def test_hard_buffer_smaller_than_soft_buffer_is_rejected(self):
        with pytest.raises(ValueError, match="geofence_hard_buffer_m"):
            SafetyLimits(geofence_soft_warning_buffer_m=15.0, geofence_hard_buffer_m=10.0)

    def test_non_positive_geofence_buffers_are_rejected(self):
        with pytest.raises(ValueError, match="must be positive"):
            SafetyLimits(geofence_soft_warning_buffer_m=0.0)


class TestShowGeofenceEnvelope:
    def test_returns_all_three_radii(self):
        show = _show({0: [{"time": 0.0, "x": 30.0, "y": 0.0, "z": -2.0}]})
        envelope = show.geofence_envelope()
        assert envelope["envelope_radius_m"] == pytest.approx(30.0)
        assert envelope["soft_warning_radius_m"] == pytest.approx(40.0)
        assert envelope["hard_geofence_radius_m"] == pytest.approx(45.0)

    def test_a_30m_static_default_would_have_false_positived(self):
        """The circle-at-N=100 scenario from issue #53: ~38m envelope.

        The static 30.0m default (DEFAULT_LIMITS.geofence_radius_m) would
        have RTL'd this show; the dynamic hard radius must clear it instead.
        """
        show = _show(
            {
                0: [{"time": 0.0, "x": 38.0, "y": 0.0, "z": -2.0}],
                1: [{"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0}],
                2: [{"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0}],
            }
        )
        envelope = show.geofence_envelope()
        assert envelope["envelope_radius_m"] > DEFAULT_LIMITS.geofence_radius_m
        assert envelope["hard_geofence_radius_m"] > envelope["envelope_radius_m"]

    def test_accepts_studio_and_gcs_shapes_identically(self):
        studio_shape = {
            "drones": [{"id": 0, "waypoints": [{"time": 0.0, "x": 12.0, "y": 0.0, "z": -2.0}]}]
        }
        gcs_shape = {0: [{"time": 0.0, "x": 12.0, "y": 0.0, "z": -2.0}]}
        assert Show.from_any(studio_shape).geofence_envelope() == Show.from_any(gcs_shape).geofence_envelope()
