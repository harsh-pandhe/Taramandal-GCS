"""Issue #156: validate_show's cost used to grow superlinearly with fleet
size - O(steps x drones x waypoints) from a linear waypoint-segment scan in
tara_core.geometry.interpolate_waypoint, making 500-1000 drone shows take
tens of seconds to minutes. #164's bisect fix turned each lookup from
O(waypoints) to O(log waypoints); test_geometry.py's own equivalence test
(test_bisect_interpolation_matches_linear_scan_including_duplicate_times)
already proves it gives identical results. This file is the other half of
#156's acceptance criteria: a scale regression guard, so a future change to
interpolate_waypoint or validate_show can't silently reintroduce the old
superlinear cost without a test catching it.

Measured (self-review, 2026-10-29, median of 5, this machine, a fixed
30s/2Hz grid show via /api/validate end to end - see the issue for the
full write-up): 100 drones ~1.7s, 500 drones ~8s, 1000 drones ~21s. Growth
is close to linear in drone count at fixed duration (500->1000 is 2x
drones for 2.7x time, not the old superlinear blow-up), which is the
"Expected" behavior the issue asks to confirm.
"""

import time

from tara_core import validate_show


def _grid_show(n: int, duration: float = 30.0, dt: float = 0.1) -> dict:
    """n drones in a grid, holding position - the workload shape that
    matters for this test is the sweep (duration/dt steps x n drones) and,
    for #156's own bug specifically, the *stored waypoint count per drone*
    (dt=0.1 matches sequencer.js's 10 Hz default build rate, #160) -
    interpolate_waypoint's pre-#164 cost was O(waypoints) per lookup, so a
    show with too few stored waypoints wouldn't meaningfully exercise the
    regression this test exists to catch, even though it has the right
    drone count and sweep step count.
    """
    times = [round(i * dt, 2) for i in range(int(duration / dt) + 1)]
    side = max(1, int(n**0.5))
    drones = []
    for i in range(n):
        x, y = (i % side) * 2.5, (i // side) * 2.5
        drones.append({
            "id": i,
            "waypoints": [{"time": t, "x": x, "y": y, "z": -10.0, "yaw": 0.0} for t in times],
        })
    return {"drones": drones}


def test_validate_show_500_drones_completes_within_a_generous_budget():
    """Not a tight performance target - a regression guard. This machine
    measures ~0.5s for this exact show (301 stored waypoints/drone, 301
    validator sweep steps, 500 drones, no violations to report); a 10s
    budget leaves ~20x margin for a loaded CI runner while still clearly
    catching a return to the old (pre-#164) linear-scan cost, which this
    workload's own waypoint count would multiply by roughly another order
    of magnitude.
    """
    show = _grid_show(500)
    start = time.perf_counter()
    report = validate_show(show)
    elapsed = time.perf_counter() - start

    assert report["status"] == "PASSED"  # a 2.5m grid clears every limit
    assert elapsed < 10.0, f"validate_show took {elapsed:.1f}s at 500 drones (budget: 10s)"
