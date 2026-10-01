"""Bulk telemetry ingestion on backend="sim" (issue #51).

The sim backend used to run the same 8 per-drone monitor tasks as px4, even
though all 8 read rows of one shared array. At N=1000 that was 8,000 always-on
tasks, and on top of that every 10Hz trajectory tick gathered 1,000 setpoint
tasks. These tests pin:

  * that the bulk path writes exactly the telemetry the per-drone monitors
    would, checked by running both against the same frozen fleet, and
  * that the total number of live asyncio tasks at N=1000 during a show stays
    under the issue's budget of 50.

The timing criteria (0% missed ticks, xRT >= 0.98 at N=1000) are measured by
tools/benchmark_fleet_scale.py rather than asserted here: they depend on the
host, and a shared CI runner would make them flaky.
"""

import asyncio
import copy
import gc

import pytest

from tara_core import altitude_layers
from tara_gcs.drone_manager import DroneManager, _pad_offset

#: Issue #51's acceptance budget for live asyncio tasks at N=1000.
TASK_BUDGET = 50


def _hover_show(n, duration=3.0):
    alts = altitude_layers(n)
    show = {}
    for i in range(n):
        x, y = _pad_offset(i)
        show[i] = [
            {"time": 0.0, "x": x, "y": y, "z": 0.0, "yaw": 0.0},
            {"time": duration, "x": x + 0.5, "y": y, "z": -alts[i], "yaw": 30.0},
        ]
    return show


@pytest.mark.asyncio
async def test_sim_backend_spawns_no_per_drone_tasks():
    small, large = DroneManager(ports=list(range(3)), backend="sim"), DroneManager(ports=list(range(300)), backend="sim")
    try:
        await small.connect_all()
        await large.connect_all()
        # fleet tick, bulk telemetry, proximity, geofence, lifecycle: whatever the size.
        assert len(small._tasks) == len(large._tasks) == 5
    finally:
        await small.shutdown()
        await large.shutdown()


class _TaskCounter:
    """Exact count of live asyncio tasks, via the loop's task factory.

    Sampling ``asyncio.all_tasks()`` is not enough: a gather() of 1,000
    coroutines that never suspend creates 1,000 tasks that are all finished
    within one loop iteration, so a sampling task almost never wakes up while
    they exist. Counting at creation and on completion sees every one.
    """

    def __init__(self, loop):
        self.live = len(asyncio.all_tasks(loop))
        self.peak = self.live
        self.created = 0
        self._loop = loop
        self._previous = loop.get_task_factory()

    def __enter__(self):
        def factory(loop, coro, **kwargs):
            task = asyncio.Task(coro, loop=loop, **kwargs)
            self.created += 1
            self.live += 1
            self.peak = max(self.peak, self.live)
            task.add_done_callback(self._done)
            return task

        self._loop.set_task_factory(factory)
        return self

    def _done(self, _task):
        self.live -= 1

    def __exit__(self, *exc):
        self._loop.set_task_factory(self._previous)


@pytest.mark.asyncio
async def test_under_50_asyncio_tasks_at_1000_drones_through_a_show_and_rtl():
    manager = DroneManager(ports=list(range(1000)), backend="sim")
    try:
        with _TaskCounter(asyncio.get_running_loop()) as counter:
            await manager.connect_all()
            await asyncio.sleep(0.2)  # first telemetry read, so the slot remap sees healthy drones
            manager.start_trajectory(_hover_show(1000), mode="LOCAL", execution="streamed")
            await asyncio.sleep(1.0)  # ~10 trajectory ticks, 20 telemetry reads
            assert manager.is_running_trajectory
            await manager.trigger_rtl()  # fleet-wide commands must not fan out either
            created_during_setup_and_show = counter.created
            print(f"N=1000: peak {counter.peak} live tasks, {counter.created} created")
    finally:
        await manager.shutdown()

    assert counter.peak < TASK_BUDGET, f"{counter.peak} live asyncio tasks at N=1000"
    # No per-tick fan-out either: tasks are created once, not every tick.
    assert created_during_setup_and_show < TASK_BUDGET


@pytest.mark.asyncio
async def test_bulk_ingestion_matches_the_per_drone_monitors():
    """Run both telemetry paths against the same frozen fleet and compare every field."""
    n = 12
    manager = DroneManager(ports=list(range(n)), backend="sim")
    try:
        await manager.connect_all()
        fleet = manager._sim_fleet
        fleet.stop()  # freeze physics so both paths see the same state
        await asyncio.sleep(0)
        for task in manager._tasks:
            task.cancel()
        await asyncio.gather(*manager._tasks, return_exceptions=True)
        manager._tasks.clear()

        fs = fleet.state
        fs.config.position_noise_std_m = 0.0
        fs.position[:, 0] = [0.123 * i for i in range(n)]
        fs.position[:, 1] = [-1.005 * i for i in range(n)]
        fs.position[:, 2] = [2.5 + 0.017 * i for i in range(n)]
        fs.armed[::3] = True
        fs.connected[5] = False
        fs.battery_pct[:] = [99.96 - 3.3 * i for i in range(n)]
        fs.satellites[4] = 3
        fs.flight_mode[7] = "OFFBOARD"
        defaults = copy.deepcopy(manager.telemetry)

        manager._ingest_bulk_telemetry(fleet.get_bulk_telemetry(), include_slow=True)
        bulk = copy.deepcopy(manager.telemetry)

        # The legacy path: the 8 per-drone monitor coroutines, each run until
        # it has written its first sample.
        manager.telemetry = copy.deepcopy(defaults)
        monitors = [
            m(d)
            for d in range(n)
            for m in (
                manager._monitor_connection, manager._monitor_battery, manager._monitor_flight_mode,
                manager._monitor_gps, manager._monitor_position, manager._monitor_armed_state,
                manager._measure_latency_loop, manager._monitor_home,
            )
        ]
        tasks = [asyncio.create_task(m) for m in monitors]
        await asyncio.sleep(0.02)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        for d in range(n):
            assert bulk[d] == manager.telemetry[d], f"drone {d}"
            for key, value in bulk[d].items():
                assert type(value) is type(manager.telemetry[d][key]), f"drone {d} {key}"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_disarm_resets_geofence_breach_like_the_armed_monitor():
    manager = DroneManager(ports=[0, 1], backend="sim")
    try:
        await manager.connect_all()
        fleet = manager._sim_fleet
        fleet.state.armed[0] = True
        manager._ingest_bulk_telemetry(fleet.get_bulk_telemetry(), include_slow=False)
        manager.geofence_breaches[0] = True

        fleet.state.armed[0] = False
        manager._ingest_bulk_telemetry(fleet.get_bulk_telemetry(), include_slow=False)

        assert manager.geofence_breaches[0] is False
        assert manager.telemetry[0]["armed"] is False
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_telemetry_is_populated_right_after_connect():
    """The first bulk read includes the slow fields, so nothing waits 0.5s for them."""
    manager = DroneManager(ports=list(range(5)), backend="sim")
    try:
        await manager.connect_all()
        await asyncio.sleep(0.1)
        for tel in manager.telemetry.values():
            assert tel["connected"] is True
            assert tel["gps_lock"] is True
            assert tel["battery_percent"] == 100.0
            assert tel["home_lat"] != 0.0
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_starting_a_show_freezes_the_pre_show_heap():
    """Full GC passes over the long-lived heap were the last source of late ticks at N=1000."""
    manager = DroneManager(ports=[0, 1], backend="sim")
    try:
        await manager.connect_all()
        gc.unfreeze()
        manager.start_trajectory(_hover_show(2), mode="LOCAL", execution="streamed")
        assert gc.get_freeze_count() > 0
    finally:
        await manager.shutdown()
        gc.unfreeze()
