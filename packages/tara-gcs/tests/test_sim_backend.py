"""The Phase 2 milestone, made durable.

`DroneManager` runs completely unchanged against `backend="sim"` — no mocks,
no MAVSDK, no PX4, no Gazebo, no subprocess, no network. A fleet of drones
connects, launches, and flies a show entirely in-process, backed by one
shared vectorized `tara_sim.FleetState` instead of 8 asyncio tasks and one
`mavsdk_server` process per drone.

Landing/RTL are exercised at the physics and adapter layers already
(`packages/tara-sim/tests/test_fleet_state.py`,
`test_system_adapter.py::TestFlightCommands`) rather than here: at n=100,
`altitude_layers` cycles hover altitudes up to ~118m, and a fixed-rate descent
from there would make "land, then assert" an unbounded wait for reasons
unrelated to what this test is actually proving. This test's job stops at
"100 drones fly a complete show safely" — a clean stopping point that doesn't
smuggle in a slow, unrelated landing-time assertion.
"""

import asyncio

import pytest

from tara_core import altitude_layers
from tara_gcs.drone_manager import DroneManager, _pad_offset


def _short_safe_show(n: int, duration: float = 6.0) -> dict:
    """A minimal trajectory: climb, hold, descend a little — each drone stays
    at its own pad/altitude the whole time. Safety comes for free here (the
    pad grid and altitude layering already clear the thresholds per
    test_layout.py), which is deliberate: this test's job is proving the sim
    pipeline runs a real show cleanly, not re-proving the generator's own
    safety math.

    Pad positions come from `_pad_offset` — the same one `DroneManager` itself
    uses, drawn from a grid fixed at `MAX_FLEET_SIZE` — not a fresh
    `launch_grid(n)` sized to this test's fleet. Those disagree: `launch_grid`'s
    column count depends on `n` (`ceil(sqrt(n))`), so a locally-sized grid
    reshuffles drones into a different arrangement than the fixed one
    `DroneManager` converts fleet-frame coordinates against — precisely the
    "a drone's pad moves as the fleet grows" trap `_pad_offset`'s own
    docstring warns about. Confirmed by running this test with `launch_grid(n)`
    first: two mid-fleet drones ended up 1.37m apart mid-show.
    """
    alts = altitude_layers(n)
    climb_t, hold_t = duration * 0.3, duration * 0.7
    show = {}
    for i in range(n):
        x, y = _pad_offset(i)
        alt = alts[i]
        show[i] = [
            {"time": 0.0, "x": x, "y": y, "z": 0.0, "yaw": 0.0},
            {"time": climb_t, "x": x, "y": y, "z": -alt, "yaw": 0.0},
            {"time": hold_t, "x": x, "y": y, "z": -alt, "yaw": 0.0},
            {"time": duration, "x": x, "y": y, "z": -alt * 0.9, "yaw": 0.0},
        ]
    return show


async def _fly_a_show(manager: DroneManager, n: int, duration: float) -> None:
    await manager.connect_all()
    await asyncio.sleep(0.5)  # let telemetry + the armable settle-delay catch up
    await manager.launch_sequence()
    manager.start_trajectory(_short_safe_show(n, duration), mode="LOCAL")
    await asyncio.sleep(duration + 1.5)


def _assert_clean_show(manager: DroneManager, n: int) -> None:
    assert not any(manager.proximity_breaches.values()), (
        f"proximity violated: {[(k, v) for k, v in manager.proximity_breaches.items() if v]}"
    )
    assert not any(manager.geofence_breaches.values()), (
        f"geofence violated: {[k for k, v in manager.geofence_breaches.items() if v]}"
    )
    assert len(manager.telemetry) == n
    assert all(tel["connected"] for tel in manager.telemetry.values())
    armed_count = sum(1 for tel in manager.telemetry.values() if tel["armed"])
    assert armed_count == n, f"expected all {n} drones armed and flying, only {armed_count} were"


@pytest.mark.asyncio
async def test_small_sim_fleet_flies_a_clean_show():
    """Fast correctness check at a representative size before the N=100 run below."""
    n = 12
    manager = DroneManager(ports=list(range(n)), backend="sim")
    try:
        await asyncio.wait_for(_fly_a_show(manager, n, duration=5.0), timeout=30.0)
        _assert_clean_show(manager, n)
    finally:
        await manager.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_100_drone_sim_show_completes_with_zero_safety_violations():
    """The milestone: 100 drones fly a complete show, zero proximity/geofence
    violations, entirely in-process, in a normal pytest run.

    Real PX4 SITL caps out near 10 drones on a 12-core/30GB dev machine (~15-25%
    of a core + 200-300MB per instance, plus one mavsdk_server gRPC process
    each). This test proves the same DroneManager code — connect, launch, fly —
    runs at 10x that fleet size with one shared physics task instead of 800
    asyncio tasks and 100 subprocesses.

    Deliberately slow (~100-150s): `launch_sequence`'s 1s-per-drone stagger is
    real production behaviour this test pays honestly rather than special-
    casing away, so what's proven is the actual code path at actual scale.
    Excluded from a quick loop with `pytest -m "not slow"`.
    """
    n = 100
    manager = DroneManager(ports=list(range(n)), backend="sim")
    try:
        await asyncio.wait_for(_fly_a_show(manager, n, duration=6.0), timeout=180.0)
        _assert_clean_show(manager, n)
    finally:
        await manager.shutdown()
