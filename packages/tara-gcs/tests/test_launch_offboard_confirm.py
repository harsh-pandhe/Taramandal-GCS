"""A launch must confirm every planned drone is up before the show can start (issue #171).

Seen on PX4 SITL (#172, run rb2-n5-r1, 5 drones): drone 3 armed but stayed in
HOLD. The GCS logged "entered OFFBOARD mode" because offboard.start() returned
without raising, and its own flight-mode telemetry never left HOLD. About 13 s
later drone 3 disarmed on the pad. The FSM then moved TAKEOFF -> FORMATION_HOLD
86 ms after drone 3 dropped out of the armed set, because formation was judged
over whichever drones were still armed. The show then streamed with drone 3 on
the ground.

These tests run the same sim-backend fleet as test_fleet_lifecycle.py, with one
drone made to behave the way drone 3 did. The policy under test is the
conservative one: any planned drone that does not get up blocks FORMATION_HOLD,
so the fleet cannot reach RUNNING. The other option, flying on without that
drone, is Harsh's call (see the PR).
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from tara_gcs import main as gcs_main
from tara_gcs.fleet_fsm import LifecycleState as S

sys.path.insert(0, str(Path(__file__).parent))
import test_fleet_lifecycle as lifecycle  # noqa: E402

# Reused from the lifecycle tests: the same sim-backend fleet and helpers.
fleet = lifecycle.fleet
_capture_info_logs = lifecycle._capture_info_logs
COMMANDER = lifecycle.COMMANDER
_connect = lifecycle._connect
_wait_for_state = lifecycle._wait_for_state

N = 3
FAULTY = 1  # hover altitude 4 m; drones 0 and 2 climb to 2 m and 6 m


def _hold_ignores_setpoints(manager, drone_id):
    """PX4 in HOLD does not fly offboard setpoints. The sim would, so drop them."""
    offboard = manager.drones[drone_id].offboard
    fs = manager._sim_fleet.state
    original = offboard.set_position_ned

    async def set_position_ned(position):
        if fs.offboard_active[drone_id]:
            await original(position)

    offboard.set_position_ned = set_position_ned


def _never_enters_offboard(manager, drone_id):
    """offboard.start() returns without raising, and the vehicle stays in HOLD."""
    async def start():
        return None

    manager.drones[drone_id].offboard.start = start
    _hold_ignores_setpoints(manager, drone_id)


def _arms_then_disarms_on_the_pad(manager, drone_id, after_s=1.0):
    """Enters OFFBOARD, never climbs, then disarms itself, like a pre-takeoff auto-disarm."""
    offboard = manager.drones[drone_id].offboard
    original = offboard.set_position_ned
    fs = manager._sim_fleet.state

    async def set_position_ned(position):
        if position.down_m < 0:  # the climb: swallowed, and the vehicle gives up
            async def auto_disarm():
                await asyncio.sleep(after_s)
                fs.command_disarm(drone_id)
            asyncio.get_running_loop().create_task(auto_disarm())
            return
        await original(position)

    offboard.set_position_ned = set_position_ned


def _record_commands(manager, drone_id):
    """Record every disarm and land the GCS sends this drone, with the altitude at the time."""
    calls = []
    action = manager.drones[drone_id].action
    for name in ("disarm", "land"):
        original = getattr(action, name)

        async def wrapper(_original=original, _name=name):
            calls.append((_name, manager._sim_fleet.state.position[drone_id, 2]))
            await _original()

        setattr(action, name, wrapper)
    return calls


async def _until(predicate, timeout_s: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return bool(predicate())


def _others_at_altitude(manager):
    from tara_gcs.drone_manager import _hover_altitude

    return all(
        manager.telemetry[d]["fleet_z"] >= _hover_altitude(d) - manager.FORMATION_ALT_TOLERANCE_M
        for d in range(N) if d != FAULTY
    )


async def _assert_show_cannot_start(manager):
    with pytest.raises(HTTPException) as exc:
        await gcs_main.start_trajectory(gcs_main.StartTrajectoryRequest(execution="streamed"), operator=COMMANDER)
    assert exc.value.status_code == 409
    assert f"Drone {FAULTY}" in str(exc.value.detail)
    assert manager.fleet_fsm.state is not S.RUNNING
    assert manager.is_running_trajectory is False


def _show(n=N):
    """Each drone holds over its own pad at its own launch altitude: nothing to transition."""
    from tara_gcs.drone_manager import _hover_altitude, _pad_offset

    show = {}
    for d in range(n):
        x, y = _pad_offset(d)
        wp = {"x": x, "y": y, "z": -_hover_altitude(d), "yaw": 0.0}
        show[d] = [{"time": 0.0, **wp}, {"time": 1.0, **wp}]
    return show


@pytest.mark.asyncio
async def test_a_drone_that_never_enters_offboard_blocks_formation_and_is_disarmed(fleet):
    manager = fleet(N)
    try:
        await _connect(manager)
        _never_enters_offboard(manager, FAULTY)
        calls = _record_commands(manager, FAULTY)

        await gcs_main.launch_fleet(operator=COMMANDER)
        await manager.launch_task
        assert await _until(lambda: _others_at_altitude(manager))
        await asyncio.sleep(1.0)  # several lifecycle polls with the healthy drones in formation

        assert manager.fleet_fsm.state is S.TAKEOFF
        # Disarmed on the pad, never sent up.
        assert manager.telemetry[FAULTY]["armed"] is False
        assert calls and calls[0][0] == "disarm" and calls[0][1] < 0.2
        assert manager._sim_fleet.state.position[FAULTY, 2] < 0.2
        assert "OFFBOARD" in manager.launch_failures[FAULTY]
        assert set(manager.launch_failures) == {FAULTY}
        await _assert_show_cannot_start(manager)

        status = await gcs_main.get_status()
        assert status["launch_failures"] == {str(FAULTY): manager.launch_failures[FAULTY]}
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_a_drone_that_arms_then_disarms_without_climbing_blocks_formation(fleet):
    """The rb2-n5-r1 sequence: armed, OFFBOARD, no climb, disarmed on the pad."""
    manager = fleet(N)
    try:
        await _connect(manager)
        _arms_then_disarms_on_the_pad(manager, FAULTY)

        await gcs_main.launch_fleet(operator=COMMANDER)
        await manager.launch_task
        assert await _until(lambda: _others_at_altitude(manager) and not manager.telemetry[FAULTY]["armed"])
        await asyncio.sleep(1.0)

        assert manager.fleet_fsm.state is S.TAKEOFF, "formation reached with a planned drone on the ground"
        assert "disarmed" in manager.launch_failures[FAULTY]
        assert set(manager.launch_failures) == {FAULTY}
        manager.active_trajectory = _show()
        await _assert_show_cannot_start(manager)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_a_healthy_fleet_reaches_formation_and_starts_the_show(fleet):
    """Unchanged behaviour: the same lifecycle, nobody disarmed or landed by the launch checks."""
    manager = fleet(N)
    try:
        await _connect(manager)
        calls = {d: _record_commands(manager, d) for d in range(N)}

        started = time.monotonic()
        await gcs_main.launch_fleet(operator=COMMANDER)
        await _wait_for_state(manager, S.FORMATION_HOLD)
        took = time.monotonic() - started

        assert all(manager.telemetry[d]["armed"] for d in range(N))
        assert all(c == [] for c in calls.values())
        # Stagger (2 s for 3 drones) + climb to 6 m; the OFFBOARD check must not add whole seconds.
        assert took < 10.0, f"formation took {took:.1f}s"

        manager.active_trajectory = _show()
        await gcs_main.start_trajectory(gcs_main.StartTrajectoryRequest(execution="streamed"), operator=COMMANDER)
        assert manager.fleet_fsm.state is S.RUNNING
        triggers = [h["trigger"] for h in manager.fleet_fsm.history]
        assert triggers[:5] == [
            "POST /api/launch", "telemetry:first_drone_armed", "telemetry:climbing",
            "telemetry:formation_altitude_reached", "POST /api/start-trajectory",
        ]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_a_healthy_fleet_reports_no_launch_failures(fleet):
    manager = fleet(N)
    try:
        await _connect(manager)
        await gcs_main.launch_fleet(operator=COMMANDER)
        await _wait_for_state(manager, S.FORMATION_HOLD)
        assert manager.launch_failures == {}
        assert (await gcs_main.get_status())["launch_failures"] == {}
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_a_climb_that_stalls_in_the_air_is_landed_not_disarmed(fleet, monkeypatch):
    """Past the takeoff budget and short of altitude: disarming would drop it."""
    manager = fleet(N)
    try:
        await _connect(manager)
        monkeypatch.setattr(manager, "TAKEOFF_BUDGET_BASE_S", 3.0)
        monkeypatch.setattr(manager, "TAKEOFF_BUDGET_S_PER_M", 0.0)
        offboard = manager.drones[FAULTY].offboard
        original = offboard.set_position_ned

        async def capped(position):  # never climbs past 1.5 m of its 4 m
            position.down_m = max(position.down_m, -1.5)
            await original(position)

        offboard.set_position_ned = capped
        calls = _record_commands(manager, FAULTY)

        await gcs_main.launch_fleet(operator=COMMANDER)
        assert await _until(lambda: FAULTY in manager.launch_failures, timeout_s=10.0)

        assert "4" in manager.launch_failures[FAULTY] and "3" in manager.launch_failures[FAULTY]
        assert await _until(lambda: calls)
        assert calls[0][0] == "land", calls
        assert all(name != "disarm" for name, _ in calls)
        await asyncio.sleep(0.5)
        assert manager.fleet_fsm.state is S.TAKEOFF
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_a_drone_taken_out_of_offboard_mid_climb_is_left_alone(fleet):
    """Someone else (pilot, PX4 failsafe) has it: no command from the GCS, but no formation either."""
    manager = fleet(N)
    try:
        await _connect(manager)
        calls = _record_commands(manager, FAULTY)
        fs = manager._sim_fleet.state

        await gcs_main.launch_fleet(operator=COMMANDER)
        assert await _until(lambda: manager.telemetry[FAULTY]["fleet_z"] > 1.0)
        original_ingest = manager._ingest_bulk_telemetry

        def ingest(snap, include_slow):
            original_ingest(snap, include_slow)
            manager.telemetry[FAULTY]["flight_mode"] = "POSCTL"

        manager._ingest_bulk_telemetry = ingest
        fs.offboard_active[FAULTY] = False

        assert await _until(lambda: FAULTY in manager.launch_failures)
        assert "POSCTL" in manager.launch_failures[FAULTY]
        await asyncio.sleep(1.0)
        assert calls == []
        assert manager.fleet_fsm.state is S.TAKEOFF
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_a_new_launch_starts_with_a_clean_slate(fleet):
    manager = fleet(N)
    try:
        await _connect(manager)
        manager.launch_failures[FAULTY] = "left over from an earlier launch"
        await gcs_main.launch_fleet(operator=COMMANDER)
        await _wait_for_state(manager, S.FORMATION_HOLD)
        assert manager.launch_failures == {}
    finally:
        await manager.shutdown()
