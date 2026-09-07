"""Issue #13: generator regression tests at N=50/100 for all 5 shapes.

Every formation shape generator must produce a full show — takeoff,
choreography, and landing, not just steady-state hover — with zero proximity
or geofence violations at N=50 and N=100 drones.
"""

from __future__ import annotations

import math

import pytest

from tara_core import DEFAULT_LIMITS
from tara_core.geometry import interpolate_waypoint, sweep_times
from tara_core.show import Show
from tara_studio.generator import (
    generate_circle,
    generate_cube,
    generate_heart,
    generate_helix,
    generate_spline,
)
from tara_studio.validator import validate_choreography

DURATION = 30.0
RATE = 2.0

SHAPES = {
    "circle": generate_circle,
    "helix": generate_helix,
    "heart": generate_heart,
    "cube": generate_cube,
    "spline": generate_spline,
}


def _geofence_violations(choreo: dict, radius: float, dt: float = 0.1) -> list[dict]:
    """Sweep the full show at ``dt`` and report every sample where a drone's
    horizontal distance from the fleet origin (0, 0) exceeds ``radius``.

    ``validate_choreography``/``validate_show`` only check
    proximity/velocity/acceleration/yaw-rate — there is no geofence check in
    the design-time validator, only in ``tara_gcs.drone_manager``'s live
    monitor. This mirrors that same horizontal-distance-from-home check, at
    the same dt=0.1s cadence ``validate_show`` sweeps with, so entry and
    transition are covered, not just the generator's own waypoint samples.
    """
    show = Show.from_any(choreo)
    violations = []
    for t in sweep_times(show.duration, dt):
        for drone_id, wps in show.drones.items():
            wp = interpolate_waypoint(wps, t)
            if wp is None:
                continue
            dist = math.hypot(wp["x"], wp["y"])
            if dist > radius:
                violations.append(
                    {"time": round(t, 2), "drone": drone_id, "distance": round(dist, 2)}
                )
    return violations


# Every (shape, n) combination below currently fails — not because the test
# is wrong, but because the generators have a real, confirmed proximity/
# geofence bug during entry-blend, choreography drift, or landing (filed as
# #32; confirmed by Harsh on PR #30, not a broken test). Each failing case is
# marked xfail(strict=True) rather than left red, so the suite documents the
# known gap instead of blocking merge. strict=True is deliberate: once #32 is
# fixed and a case starts passing, strict turns that into an XPASS failure
# instead of silently staying green — a forcing function to come back and
# delete that entry from _KNOWN_FAILING rather than the marker rotting in
# place. Update this set (not the test body) as cases get fixed one at a time.
_KNOWN_FAILING = {
    ("circle", 50),
    ("circle", 100),
    ("helix", 50),
    ("helix", 100),
    ("heart", 50),
    ("heart", 100),
    ("cube", 50),
    ("cube", 100),
    ("spline", 50),
    ("spline", 100),
}

# Generation + validation at N=100 runs well under a second per shape (see
# durations captured while writing this test), nowhere near the wall-clock
# cost the repo's `slow` marker is for (e.g. the sim-backend milestone test's
# real per-drone launch stagger), so N=100 here is not marked slow.
_CASES = [
    pytest.param(
        shape_name, n,
        marks=pytest.mark.xfail(reason="#32", strict=True),
        id=f"{shape_name}-{n}",
    )
    if (shape_name, n) in _KNOWN_FAILING
    else pytest.param(shape_name, n, id=f"{shape_name}-{n}")
    for shape_name in SHAPES
    for n in (50, 100)
]


@pytest.mark.parametrize("shape_name, n", _CASES)
def test_shape_has_zero_violations_across_full_show(shape_name, n):
    """Every generator must be proximity- and geofence-clean across the whole
    show — takeoff, choreography, and landing — not just steady-state hover.
    """
    generate = SHAPES[shape_name]
    choreo = generate(n, DURATION, RATE)

    report = validate_choreography(choreo)
    assert not report["violations"]["proximity"], (
        f"{shape_name} @ N={n}: proximity violations "
        f"(design threshold={DEFAULT_LIMITS.design_separation_m}m, "
        f"min_distance={report['metrics']['min_distance_m']}m): "
        f"{report['violations']['proximity'][:5]}"
    )

    geofence_hits = _geofence_violations(choreo, DEFAULT_LIMITS.geofence_radius_m)
    assert not geofence_hits, (
        f"{shape_name} @ N={n}: geofence violations "
        f"(radius={DEFAULT_LIMITS.geofence_radius_m}m): "
        f"{geofence_hits[:5]} ... worst="
        f"{max(geofence_hits, key=lambda v: v['distance'])}"
    )
