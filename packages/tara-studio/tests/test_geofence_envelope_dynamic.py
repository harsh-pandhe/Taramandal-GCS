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
    # And it must actually be doing dynamic work, not coincidentally landing
    # back on the static default — these shapes are the ones that needed it.
    from tara_core import DEFAULT_LIMITS

    assert envelope["envelope_radius_m"] > DEFAULT_LIMITS.geofence_radius_m, (
        f"{shape_name} @ N={n}: expected this shape to exceed the static "
        f"{DEFAULT_LIMITS.geofence_radius_m}m default (that's the bug #53 fixes); "
        f"got envelope={envelope['envelope_radius_m']:.2f}m — test fixture may be stale"
    )
