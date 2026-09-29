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


def _altitude_violations(choreo: dict, ceiling: float, dt: float = 0.1) -> list[dict]:
    """Every sample where a drone is above ``ceiling`` or below the ground.

    Swept at the same cadence as the geofence check, so it covers the
    interpolated path between waypoints, not only the waypoints themselves.
    """
    show = Show.from_any(choreo)
    violations = []
    for t in sweep_times(show.duration, dt):
        for drone_id, wps in show.drones.items():
            wp = interpolate_waypoint(wps, t)
            if wp is None:
                continue
            altitude = -wp["z"]  # NED: z is down
            if altitude > ceiling or altitude < 0.0:
                violations.append({"time": round(t, 2), "drone": drone_id, "altitude": round(altitude, 2)})
    return violations


# All ten cases used to include six strict xfails (#32): circle, helix and
# heart were centred at (5, n-1), 45-200 m from the fleet origin, and at N=100
# helix breached separation on its landing legs. Formations are now centred on
# the origin and bounded inside the geofence (see generator.FORMATION_CENTER),
# so every case is expected to pass outright.
#
# Generation + validation at N=100 runs in about a second per shape, nowhere
# near the wall-clock cost the repo's `slow` marker is for, so N=100 here is
# not marked slow.
_CASES = [
    pytest.param(shape_name, n, id=f"{shape_name}-{n}")
    for shape_name in SHAPES
    for n in (50, 100)
]


@pytest.mark.parametrize("shape_name, n", _CASES)
def test_shape_has_zero_violations_across_full_show(shape_name, n):
    """Every generator must be proximity-, geofence- and altitude-clean across
    the whole show — takeoff, choreography, and landing — not just
    steady-state hover.
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

    altitude_hits = _altitude_violations(choreo, DEFAULT_LIMITS.max_altitude_m)
    assert not altitude_hits, (
        f"{shape_name} @ N={n}: altitude violations "
        f"(ground to {DEFAULT_LIMITS.max_altitude_m}m): {altitude_hits[:5]}"
    )


def test_helix_100_paired_layer_drones_keep_the_design_separation():
    """Regression for the #72 x #73 interaction, not just the general sweep
    above.

    #73 pairs same-altitude-layer drones diametrically opposite on the helix
    ring so their straight blend/landing legs run side by side. #72
    separately replaced the phase-blend easing (smoothstep -> a minimum-jerk
    quintic, for jerk continuity, issue #60). Neither change was wrong on
    its own -- #73's own suite passed before #72 existed, and #72 touches an
    unrelated function (_ease) -- but merged together they interacted: the
    ring's rotation during blend was driven by raw elapsed time while the
    pad->target blend weight was eased, so changing the easing curve's
    *shape* (not just its endpoints) shifted how far the ring had turned by
    a given blend fraction, bringing a paired pair (drones 12 & 71 at
    n=100) to 1.35m -- under the 2.0m design separation -- during the
    entry blend, not the landing leg. Fixed in generator.py by deriving the
    ring's blend-phase rotation from the actual current easing curve
    (self-adapting, not tied to smoothstep's specific shape) and by
    replacing the pair-orientation heuristic (a static dot product, which
    disagreed with a real simulation on 25 of this fleet size's 41 paired
    groups) with a direct simulation against both the entry blend and the
    landing return leg.

    This is deliberately independent of the general N=50/100 sweep above:
    that sweep would also have caught a full regression, but a partial one
    -- a fix that solves the specific pair this bug was found with while
    leaving a different pair marginal -- could still slip through a report
    that only asserts "no violations" without pinning the actual number.
    """
    from tara_studio.generator import generate_helix

    choreo = generate_helix(100, DURATION, RATE)
    report = validate_choreography(choreo)
    min_distance = report["metrics"]["min_distance_m"]
    assert min_distance >= DEFAULT_LIMITS.design_separation_m, (
        f"helix @ N=100: minimum pairwise separation regressed to "
        f"{min_distance}m (design threshold "
        f"{DEFAULT_LIMITS.design_separation_m}m) -- the #72 x #73 easing/"
        f"pairing interaction this test pins is back."
    )
