"""Issue #53: dynamic geofence envelope clears the shapes the static 30m
radius false-positived on.

`test_generator_regression.py` documents (via strict xfail) that circle,
heart and helix at N=50/100 fail a *static* 30m geofence sweep because their
formation centre sits 45-103m from the fleet origin — a known, separately
tracked gap (`fix/launch-pad-geofence`), not something this issue fixes.

What issue #53 *does* fix: once the geofence radius is derived from the
show's own computed envelope (`Show.geofence_envelope()`) instead of a fixed
30m, the same shapes must no longer trip it — the hard radius is defined to
sit outside every waypoint of the actual show by construction.
"""

from __future__ import annotations

import math

import pytest

from tara_core.geometry import interpolate_waypoint, sweep_times
from tara_core.show import Show
from tara_studio.generator import generate_circle, generate_heart, generate_helix

DURATION = 30.0
RATE = 2.0

SHAPES = {
    "circle": generate_circle,
    "heart": generate_heart,
    "helix": generate_helix,
}


def _max_swept_radius(choreo: dict, dt: float = 0.1) -> float:
    """The true max horizontal distance from origin across the whole show,
    sampled at the same dt the live-monitor-equivalent sweep in
    test_generator_regression.py uses — not just the generator's own stored
    waypoints, so interpolated in-between positions are covered too.
    """
    show = Show.from_any(choreo)
    worst = 0.0
    for t in sweep_times(show.duration, dt):
        for wps in show.drones.values():
            wp = interpolate_waypoint(wps, t)
            if wp is None:
                continue
            worst = max(worst, math.hypot(wp["x"], wp["y"]))
    return worst


@pytest.mark.parametrize("shape_name", ["circle", "heart"])
@pytest.mark.parametrize("n", [50, 100])
def test_dynamic_hard_radius_clears_the_whole_swept_show(shape_name, n):
    """The exact cases `test_generator_regression.py` xfails against a static
    30m radius must pass once the radius is this show's own envelope + buffer.
    """
    generate = SHAPES[shape_name]
    choreo = generate(n, DURATION, RATE)

    show = Show.from_any(choreo)
    envelope = show.geofence_envelope()

    swept_max = _max_swept_radius(choreo)
    assert swept_max <= envelope["hard_geofence_radius_m"], (
        f"{shape_name} @ N={n}: swept max distance {swept_max:.2f}m exceeds "
        f"the computed hard geofence radius {envelope['hard_geofence_radius_m']:.2f}m "
        f"(envelope={envelope['envelope_radius_m']:.2f}m)"
    )
    # This used to also assert envelope_radius_m > the static 30m default,
    # to prove the dynamic path was doing real work rather than coincidentally
    # landing on the default. That assumption is now stale: #73 (issue #32)
    # fixed these generators to keep every formation inside the geofence on
    # purpose, so circle/heart at N=50/100 no longer reach 30m at all (28.28m
    # / 29.73m — bounded by the launch-pad grid's own footprint, not the
    # shape). That is the *intended* outcome of #73, not evidence #53's
    # dynamic computation stopped doing anything: see
    # test_envelope_reflects_a_genuinely_out_of_bounds_trajectory below for a
    # fixture that isolates and directly tests the dynamic-expansion
    # behavior itself, independent of what any particular generator produces.


def test_envelope_reflects_a_genuinely_out_of_bounds_trajectory():
    """Directly tests the raw-waypoint-derived envelope behavior issue #53
    fixes, with a hand-built trajectory rather than a generator's output.

    Replaces the previous fixture's reliance on circle/heart at N=50/100
    genuinely exceeding the static 30m default: #73 fixed those generators
    to no longer do that (on purpose — see the note above), which is a
    generator-geometry change, not a geofence-logic one, and left that
    fixture's "must exceed 30m" assumption stale. This constructs a show
    with one drone at a known, exact 40m from the origin, so the correct
    envelope/hard-radius values are known exactly rather than inferred from
    whatever a shape generator happens to currently produce -- and remains
    valid regardless of future changes to any generator's geometry.
    """
    from tara_core import DEFAULT_LIMITS
    from tara_core.show import Show

    far_radius_m = 40.0  # > DEFAULT_LIMITS.geofence_radius_m (30m) by construction
    choreo = {
        "drones": [
            {
                "id": 0,
                "waypoints": [
                    {"time": 0.0, "x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0},
                    {"time": 10.0, "x": far_radius_m, "y": 0.0, "z": -5.0, "yaw": 0.0},
                    {"time": 20.0, "x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0},
                ],
            },
        ],
    }

    show = Show.from_any(choreo)
    envelope = show.geofence_envelope()

    assert envelope["envelope_radius_m"] == pytest.approx(far_radius_m), (
        f"expected the envelope to be exactly this trajectory's known "
        f"{far_radius_m}m extent, got {envelope['envelope_radius_m']:.2f}m — "
        f"the envelope is not being derived from the raw waypoints"
    )
    assert envelope["envelope_radius_m"] > DEFAULT_LIMITS.geofence_radius_m
    assert envelope["hard_geofence_radius_m"] == pytest.approx(
        far_radius_m + DEFAULT_LIMITS.geofence_hard_buffer_m
    )
    assert envelope["soft_warning_radius_m"] == pytest.approx(
        far_radius_m + DEFAULT_LIMITS.geofence_soft_warning_buffer_m
    )
    # And the embedded static default must not have been used instead.
    assert envelope["hard_geofence_radius_m"] != pytest.approx(
        DEFAULT_LIMITS.geofence_radius_m + DEFAULT_LIMITS.geofence_hard_buffer_m
    )
