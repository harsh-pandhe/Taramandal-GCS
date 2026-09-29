"""Unit tests for formation_bounds module: geofence checking, scaling, and rejection."""

import pytest
from tara_core import DEFAULT_LIMITS
from tara_studio.formation_bounds import (
    FormationBounds,
    check_formation_geofence,
    fit_formation_to_geofence,
)


def test_formation_bounds_clean():
    # 4 points centered at origin, radius 10m, altitude 15m
    pts = [
        {"x": 10.0, "y": 0.0, "z": -15.0},
        {"x": -10.0, "y": 0.0, "z": -15.0},
        {"x": 0.0, "y": 10.0, "z": -15.0},
        {"x": 0.0, "y": -10.0, "z": -15.0},
    ]
    bounds = FormationBounds.from_points(pts)
    assert bounds.max_radius_m == pytest.approx(10.0)
    assert bounds.max_altitude_m == pytest.approx(15.0)
    assert bounds.min_altitude_m == pytest.approx(15.0)
    assert check_formation_geofence(pts) == []


def test_formation_geofence_breach_detected():
    # Point at radius 35m > 30m
    pts = [
        {"x": 35.0, "y": 0.0, "z": -10.0},
        {"x": 0.0, "y": 0.0, "z": -10.0},
    ]
    errors = check_formation_geofence(pts)
    assert len(errors) == 1
    assert "exceeds geofence radius" in errors[0]


def test_formation_altitude_ceiling_breach():
    # Altitude 130m > 120m
    pts = [{"x": 0.0, "y": 0.0, "z": -130.0}]
    errors = check_formation_geofence(pts)
    assert any("altitude ceiling" in e for e in errors)


def test_formation_ground_penetration():
    # NED z = 1.0 is 1m underground
    pts = [{"x": 0.0, "y": 0.0, "z": 1.0}]
    errors = check_formation_geofence(pts)
    assert any("penetrates ground" in e for e in errors)


def test_fit_formation_reposition_to_fit():
    # Center is at (25, 25), radius 10m -> max distance sqrt(35^2 + 25^2) > 40m > 30m
    # But centered at (0, 0), radius is only 10m <= 30m. Repositioning should make it fit!
    pts = [
        {"x": 35.0, "y": 25.0, "z": -10.0},
        {"x": 15.0, "y": 25.0, "z": -10.0},
        {"x": 25.0, "y": 35.0, "z": -10.0},
        {"x": 25.0, "y": 15.0, "z": -10.0},
    ]
    fitted = fit_formation_to_geofence(pts)
    new_bounds = FormationBounds.from_points(fitted)
    assert new_bounds.max_radius_m <= DEFAULT_LIMITS.geofence_radius_m
    assert new_bounds.center_x == pytest.approx(0.0)
    assert new_bounds.center_y == pytest.approx(0.0)


def test_fit_formation_rejects_when_separation_cannot_be_preserved():
    # 200 drones placed along a 50m ring with tight spacing (1.57m).
    # Scaling to 29m would compress drones to ~0.91m, below min_separation (1.5m).
    # Must raise ValueError, never silently compress.
    import math
    n = 200
    r = 50.0  # large ring
    pts = [
        {"x": r * math.cos(2 * math.pi * i / n), "y": r * math.sin(2 * math.pi * i / n), "z": -10.0}
        for i in range(n)
    ]
    with pytest.raises(ValueError, match="minimum separation"):
        fit_formation_to_geofence(pts, min_separation=1.5)
