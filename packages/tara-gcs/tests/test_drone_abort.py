"""Single-drone abort (issue #59): per-drone RTL/LAND/ESTOP, the descent-path
clearance check, and the rest of the fleet flying on undisturbed.

Unit tests follow test_drone_manager.py's style (a `DroneManager(ports=[])`
with hand-built telemetry and `AsyncMock` drones). The acceptance test at the
bottom flies a real small sim fleet, same as test_sim_backend.py.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from tara_core import DEFAULT_LIMITS, altitude_layers
from tara_gcs import main as gcs_main
from tara_gcs.auth import Operator, Role
from tara_gcs.drone_manager import DroneManager, _pad_offset, abort_clearance


# Issue #64: route handlers called directly take the operator that the auth
# dependency would otherwise inject.
COMMANDER = Operator("test-commander", Role.FLIGHT_COMMANDER)


def _tel(drone_id, x=0.0, y=0.0, z=5.0, armed=True):
    return {
        "connected": True,
        "armed": armed,
        "battery_percent": 90.0,
        "satellites": 10,
        "fleet_x": x,
        "fleet_y": y,
        "fleet_z": z,
        "planned_valid": True,
        "aborted": None,
    }


def _manager_with_mock_drones(n):
    """Drones spread far apart and 10m apart in altitude, each lined up with
    its own pad's y, so no modelled abort path (including RTL's leg home and
    the descent over a pad) comes near a neighbour unless a test moves one."""
    manager = DroneManager(ports=[])
    manager.drones = {i: AsyncMock() for i in range(n)}
    manager.telemetry = {
        i: _tel(i, x=20.0 * (i + 1), y=_pad_offset(i)[1], z=5.0 + 10.0 * i) for i in range(n)
    }
    return manager


# --- Per-drone command primitives -------------------------------------------

async def test_rtl_one_leaves_offboard_then_commands_rtl():
    manager = _manager_with_mock_drones(1)
    assert await manager._rtl_one(0) is None
    manager.drones[0].offboard.stop.assert_awaited_once()
    manager.drones[0].action.return_to_launch.assert_awaited_once()


async def test_land_one_leaves_offboard_then_commands_land():
    manager = _manager_with_mock_drones(1)
    assert await manager._land_one(0) is None
    manager.drones[0].offboard.stop.assert_awaited_once()
    manager.drones[0].action.land.assert_awaited_once()


async def test_estop_one_kills_without_leaving_offboard_first():
    manager = _manager_with_mock_drones(1)
    assert await manager._estop_one(0) is None
    manager.drones[0].action.kill.assert_awaited_once()
    manager.drones[0].offboard.stop.assert_not_called()


@pytest.mark.parametrize("method, action", [
    ("_rtl_one", "return_to_launch"),
    ("_land_one", "land"),
    ("_estop_one", "kill"),
])
async def test_one_primitives_report_vehicle_rejection_instead_of_raising(method, action):
    manager = _manager_with_mock_drones(1)
    getattr(manager.drones[0].action, action).side_effect = Exception("COMMAND_DENIED")
    assert await getattr(manager, method)(0) == "COMMAND_DENIED"


async def test_one_primitives_ignore_offboard_stop_failure():
    manager = _manager_with_mock_drones(1)
    manager.drones[0].offboard.stop.side_effect = Exception("not in offboard")
    assert await manager._land_one(0) is None
    manager.drones[0].action.land.assert_awaited_once()


# --- Fleet-wide triggers: unchanged behaviour --------------------------------

@pytest.mark.parametrize("trigger, action", [("trigger_rtl", "return_to_launch"), ("trigger_land", "land")])
async def test_fleet_trigger_still_commands_every_drone_and_stops_the_show(trigger, action):
    manager = _manager_with_mock_drones(3)
    # One drone rejecting the command must not stop it reaching the others.
    getattr(manager.drones[1].action, action).side_effect = Exception("denied")
    manager.stop_trajectory = lambda: setattr(manager, "stopped", True)

    await getattr(manager, trigger)()

    assert manager.stopped is True
    for drone in manager.drones.values():
        drone.offboard.stop.assert_awaited_once()
        getattr(drone.action, action).assert_awaited_once()
    # Fleet-wide RTL/land is not a single-drone abort.
    assert not any(t["aborted"] for t in manager.telemetry.values())


# --- abort_drone --------------------------------------------------------------

@pytest.mark.parametrize("mode, action", [("rtl", "return_to_launch"), ("land", "land"), ("estop", "kill")])
async def test_abort_drone_commands_only_that_drone_and_leaves_the_show_running(mode, action):
    manager = _manager_with_mock_drones(3)
    manager.stop_trajectory = lambda: pytest.fail("single-drone abort must not stop the show")

    result = await manager.abort_drone(1, mode)

    assert result["commanded"] is True and result["error"] is None
    assert result["drone_id"] == 1 and result["mode"] == mode
    getattr(manager.drones[1].action, action).assert_awaited_once()
    for other in (0, 2):
        getattr(manager.drones[other].action, action).assert_not_called()
        manager.drones[other].offboard.stop.assert_not_called()
    assert manager.telemetry[1]["aborted"]["type"] == mode
    assert manager.telemetry[1]["planned_valid"] is False
    assert manager.telemetry[0]["aborted"] is None and manager.telemetry[2]["aborted"] is None
    assert result["clearance"]["clear"] is True  # neighbours are 10m apart
    assert result["warning"] is None


async def test_abort_drone_reports_rejection_but_keeps_the_drone_out_of_the_show():
    manager = _manager_with_mock_drones(2)
    manager.drones[0].action.land.side_effect = Exception("COMMAND_DENIED")

    result = await manager.abort_drone(0, "land")

    assert result["commanded"] is False
    assert result["error"] == "COMMAND_DENIED"
    # Offboard has been stopped and playback won't command it again, so it is
    # out of the show whether or not the vehicle accepted LAND.
    assert manager.telemetry[0]["aborted"]["type"] == "land"


async def test_abort_drone_can_escalate_an_already_aborted_drone():
    manager = _manager_with_mock_drones(1)
    await manager.abort_drone(0, "rtl")
    await manager.abort_drone(0, "estop")
    manager.drones[0].action.kill.assert_awaited_once()
    assert manager.telemetry[0]["aborted"]["type"] == "estop"


async def test_abort_drone_rejects_unknown_drone_and_mode():
    manager = _manager_with_mock_drones(1)
    with pytest.raises(KeyError):
        await manager.abort_drone(7, "land")
    with pytest.raises(ValueError):
        await manager.abort_drone(0, "hover")
    assert manager.telemetry[0]["aborted"] is None


async def test_abort_drone_warns_when_its_path_crosses_a_neighbour():
    manager = _manager_with_mock_drones(2)
    manager.telemetry[0].update(fleet_x=0.0, fleet_y=0.0, fleet_z=10.0)
    manager.telemetry[1].update(fleet_x=1.0, fleet_y=0.0, fleet_z=4.0)  # hovering under drone 0

    result = await manager.abort_drone(0, "land")

    assert result["commanded"] is True  # a warning never blocks the abort
    manager.drones[0].action.land.assert_awaited_once()
    assert result["clearance"]["clear"] is False
    assert result["clearance"]["conflicts"][0]["drone_id"] == 1
    assert "Drone 1" in result["warning"]


# --- Aborted drones are out of playback, remapping and default launches ------

async def test_send_setpoint_skips_a_drone_aborted_after_the_tick_was_built():
    manager = _manager_with_mock_drones(1)
    send = AsyncMock()
    manager.telemetry[0]["aborted"] = {"type": "land", "at": 0.0}
    await manager._send_setpoint(0, send)
    send.assert_not_called()


async def test_playback_loop_stops_commanding_an_aborted_drone():
    manager = _manager_with_mock_drones(2)
    manager.telemetry[0]["aborted"] = {"type": "land", "at": 0.0}
    manager.telemetry[0]["planned_valid"] = False
    wps = [{"time": 0.0, "x": 0.0, "y": 0.0, "z": -5.0, "yaw": 0.0},
           {"time": 5.0, "x": 1.0, "y": 0.0, "z": -5.0, "yaw": 0.0}]
    manager.is_running_trajectory = True

    task = asyncio.create_task(manager._run_trajectory_loop({0: wps, 1: wps}))
    await asyncio.sleep(0.25)
    assert manager.telemetry[0]["planned_valid"] is False
    assert manager.telemetry[1]["planned_valid"] is True
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    manager.drones[0].offboard.set_position_velocity_acceleration_ned.assert_not_called()
    assert manager.drones[1].offboard.set_position_velocity_acceleration_ned.await_count >= 2


def test_remap_treats_aborted_drone_as_unavailable():
    manager = _manager_with_mock_drones(3)
    manager.telemetry[0]["aborted"] = {"type": "rtl", "at": 0.0}
    remapped = manager.remap_trajectory_to_healthy_drones({0: ["a"], 1: ["b"]})
    assert remapped == {1: ["a"], 2: ["b"]}


async def test_default_launch_skips_aborted_drones_but_explicit_launch_targets_them():
    manager = _manager_with_mock_drones(3)
    manager.launch_gate = MagicMock(return_value={"ok": True, "armable": [0, 1, 2]})
    manager.telemetry[1]["aborted"] = {"type": "land", "at": 0.0}
    manager._launch_one = AsyncMock()

    await manager.launch_sequence()
    assert sorted(c.args[0] for c in manager._launch_one.await_args_list) == [0, 2]

    manager._launch_one.reset_mock()
    await manager.launch_sequence([1])
    assert [c.args[0] for c in manager._launch_one.await_args_list] == [1]


# --- Clearance inputs come from the remapped trajectory ----------------------

async def test_clearance_inputs_use_remapped_trajectory_and_skip_inactive_neighbours():
    manager = _manager_with_mock_drones(4)
    slot_wps = [{"time": 0.0, "x": 99.0, "y": 99.0, "z": -5.0}]
    flown_wps = [{"time": 0.0, "x": 1.0, "y": 1.0, "z": -5.0}]
    # Slot 1 was remapped onto drone 2: drone 2 flies what active_trajectory calls slot 1.
    manager.active_trajectory = {1: flown_wps, 2: slot_wps}
    manager.playing_trajectory = {2: flown_wps}
    manager.is_running_trajectory = True
    manager._playback_start_time = asyncio.get_running_loop().time()
    manager.telemetry[1]["armed"] = False
    manager.telemetry[3]["aborted"] = {"type": "rtl", "at": 0.0}

    inputs = manager._abort_clearance_inputs(0)

    assert set(inputs["others"]) == {2}
    assert inputs["others"][2] == {"waypoints": flown_wps}
    assert inputs["pad"] == _pad_offset(0)


def test_clearance_inputs_hold_neighbours_at_telemetry_when_no_show_plays():
    manager = _manager_with_mock_drones(2)
    manager.playing_trajectory = {1: [{"time": 0.0, "x": 50.0, "y": 0.0, "z": -5.0}]}
    inputs = manager._abort_clearance_inputs(0)
    assert inputs["show_elapsed_s"] is None
    assert inputs["others"][1] == {"position": (40.0, _pad_offset(1)[1], 15.0)}


# --- abort_clearance: the space-time check itself -----------------------------

def _still(x, y, alt):
    return {"position": (x, y, alt)}


def _flyby(t_cross, x, y_speed, alt_ned, t_end=100.0):
    """A drone flying along +y at constant speed, crossing y=0 at t_cross."""
    return {"waypoints": [
        {"time": 0.0, "x": x, "y": -y_speed * t_cross, "z": alt_ned},
        {"time": t_end, "x": x, "y": y_speed * (t_end - t_cross), "z": alt_ned},
    ]}


def test_default_threshold_is_the_design_separation():
    result = abort_clearance("land", (0.0, 0.0, 5.0), (0.0, 0.0), {}, None)
    assert result["threshold_m"] == DEFAULT_LIMITS.design_separation_m == 2.0
    assert result["clear"] is True


def test_land_column_conflicts_with_a_drone_hovering_beneath_it():
    result = abort_clearance("land", (0.0, 0.0, 10.0), (0.0, 0.0), {1: _still(1.0, 0.0, 3.0)}, None)
    assert [c["drone_id"] for c in result["conflicts"]] == [1]
    assert result["conflicts"][0]["min_distance_m"] == pytest.approx(1.0, abs=0.01)


def test_land_column_is_clear_of_a_drone_well_to_the_side():
    result = abort_clearance("land", (0.0, 0.0, 10.0), (0.0, 0.0), {1: _still(5.0, 0.0, 3.0)}, None)
    assert result["clear"] is True


def test_crossing_is_judged_in_space_and_time():
    # Aborter lands from 10m at (0,0): at 0.5-1.5 m/s it's near 9m after ~1s
    # and on the ground within 20s. A drone crossing the column at 9m altitude
    # after 1s of show time meets it there...
    start, pad = (0.0, 0.0, 10.0), (0.0, 0.0)
    early = abort_clearance("land", start, pad, {1: _flyby(1.0, 0.0, 2.0, -9.0)}, show_elapsed_s=0.0)
    assert not early["clear"]
    # ...the same crossing 60s later finds the column empty.
    late = abort_clearance("land", start, pad, {1: _flyby(60.0, 0.0, 2.0, -9.0)}, show_elapsed_s=0.0)
    assert late["clear"]


def test_waypoint_z_is_ned_down_not_altitude():
    # Waypoint z=-9 is 9m up: inside the column. Read as altitude it'd be
    # underground and clear.
    hover = {"waypoints": [{"time": 0.0, "x": 0.5, "y": 0.0, "z": -9.0}]}
    result = abort_clearance("land", (0.0, 0.0, 10.0), (0.0, 0.0), {1: hover}, show_elapsed_s=0.0)
    assert not result["clear"]


def test_show_time_offsets_where_neighbours_are_sampled():
    # The neighbour crosses at show time 31s; aborting at show time 30s puts
    # that crossing 1s into the descent, while the aborter is still near 9m.
    result = abort_clearance("land", (0.0, 0.0, 10.0), (0.0, 0.0), {1: _flyby(31.0, 0.0, 2.0, -9.0)},
                             show_elapsed_s=30.0)
    assert not result["clear"]
    assert result["conflicts"][0]["in_s"] == pytest.approx(1.0, abs=0.3)


def test_rtl_checks_the_horizontal_leg_home():
    start, pad = (10.0, 0.0, 5.0), (0.0, 0.0)
    on_leg = abort_clearance("rtl", start, pad, {1: _still(5.0, 0.5, 5.0)}, None)
    assert not on_leg["clear"]
    well_above = abort_clearance("rtl", start, pad, {1: _still(5.0, 0.0, 12.0)}, None)
    assert well_above["clear"]
    # LAND from the same point never goes near (5, 0.5): the leg is RTL-only.
    assert abort_clearance("land", start, pad, {1: _still(5.0, 0.5, 5.0)}, None)["clear"]


def test_rtl_checks_the_descent_over_the_pad():
    result = abort_clearance("rtl", (10.0, 0.0, 5.0), (0.0, 0.0), {1: _still(0.0, 1.0, 2.0)}, None)
    assert [c["drone_id"] for c in result["conflicts"]] == [1]


def test_estop_fall_column_conflicts_with_a_drone_below():
    result = abort_clearance("estop", (0.0, 0.0, 20.0), (0.0, 0.0), {1: _still(0.5, 0.0, 5.0)}, None)
    assert not result["clear"]
    assert result["horizon_s"] < 3.0  # a fall, not a descent


def test_sampling_pad_catches_a_closest_approach_between_samples():
    # A 19 m/s drone passing 1.9m from the column crosses between the t=0 and
    # t=0.1 samples, both ~2.12m away. Its true closest approach is 1.9m.
    fast = _flyby(0.05, 1.9, 19.0, -10.0)
    result = abort_clearance("land", (0.0, 0.0, 10.0), (0.0, 0.0), {1: fast}, show_elapsed_s=0.0)
    assert not result["clear"]


def test_conflicts_are_sorted_closest_first():
    others = {1: _still(1.5, 0.0, 5.0), 2: _still(0.5, 0.0, 5.0), 3: _still(30.0, 0.0, 5.0)}
    result = abort_clearance("land", (0.0, 0.0, 10.0), (0.0, 0.0), others, None)
    assert [c["drone_id"] for c in result["conflicts"]] == [2, 1]


def test_rejects_unknown_mode():
    with pytest.raises(ValueError):
        abort_clearance("hover", (0.0, 0.0, 5.0), (0.0, 0.0), {}, None)


# --- Endpoints -----------------------------------------------------------------

@pytest.fixture
def patched_manager(monkeypatch):
    def _install(manager):
        monkeypatch.setattr(gcs_main, "drone_manager", manager)
        return manager
    return _install


@pytest.mark.parametrize("route, mode", [
    (gcs_main.abort_drone_rtl, "rtl"),
    (gcs_main.abort_drone_land, "land"),
    (gcs_main.abort_drone_estop, "estop"),
])
async def test_abort_endpoints_return_success_with_clearance(patched_manager, route, mode):
    patched_manager(_manager_with_mock_drones(2))
    body = await route(1, operator=COMMANDER)
    assert body["status"] == "success"
    assert body["mode"] == mode and body["drone_id"] == 1
    assert body["clearance"]["clear"] is True
    assert body["warning"] is None


async def test_abort_endpoint_404s_for_unknown_drone(patched_manager):
    patched_manager(_manager_with_mock_drones(1))
    with pytest.raises(HTTPException) as exc:
        await gcs_main.abort_drone_land(5, operator=COMMANDER)
    assert exc.value.status_code == 404


async def test_abort_endpoint_502s_when_the_vehicle_rejects(patched_manager):
    manager = patched_manager(_manager_with_mock_drones(1))
    manager.drones[0].action.return_to_launch.side_effect = Exception("COMMAND_DENIED")
    with pytest.raises(HTTPException) as exc:
        await gcs_main.abort_drone_rtl(0, operator=COMMANDER)
    assert exc.value.status_code == 502
    assert exc.value.detail["error"] == "COMMAND_DENIED"
    assert "clearance" in exc.value.detail


async def test_abort_endpoint_returns_the_collision_warning(patched_manager):
    manager = patched_manager(_manager_with_mock_drones(2))
    manager.telemetry[0].update(fleet_x=0.0, fleet_y=0.0, fleet_z=10.0)
    manager.telemetry[1].update(fleet_x=1.0, fleet_y=0.0, fleet_z=4.0)
    body = await gcs_main.abort_drone_land(0, operator=COMMANDER)
    assert body["status"] == "success"
    assert body["warning"] and "Drone 1" in body["warning"]


async def test_status_lists_aborted_drones(patched_manager):
    manager = patched_manager(_manager_with_mock_drones(3))
    assert (await gcs_main.get_status())["aborted"] == []
    await manager.abort_drone(2, "land")
    assert (await gcs_main.get_status())["aborted"] == [2]


# --- Acceptance: small sim fleet ----------------------------------------------

async def _wait_for(predicate, timeout):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.1)


def _hover_show(n, duration):
    """Each drone climbs to its own layer over its own pad and holds there."""
    alts = altitude_layers(n)
    show = {}
    for i in range(n):
        x, y = _pad_offset(i)
        show[i] = [
            {"time": 0.0, "x": x, "y": y, "z": 0.0, "yaw": 0.0},
            {"time": 1.5, "x": x, "y": y, "z": -alts[i], "yaw": 0.0},
            {"time": duration, "x": x, "y": y, "z": -alts[i], "yaw": 0.0},
        ]
    return show


async def test_sim_abort_lands_one_drone_while_the_rest_fly_the_show(patched_manager):
    n = 4
    manager = patched_manager(DroneManager(ports=list(range(n)), backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)
        show = _hover_show(n, duration=20.0)
        await manager.upload_show(show)
        await manager.launch_sequence()
        manager.start_trajectory(show, mode="LOCAL", execution="streamed")
        # This show starts on the pad, so the start first flies the fleet from
        # its hold altitudes to it (issue #172 F3); the show proper begins after
        # that leg. Wait for the show to take over, climb and hold.
        await asyncio.sleep(manager.start_bridge_s + 3.0)

        # Count every setpoint the playback loop sends drone 0 from here on.
        offboard0 = manager.drones[0].offboard
        real_send = offboard0.set_position_velocity_acceleration_ned
        sent_to_0 = []

        async def counting_send(*args):
            sent_to_0.append(args)
            await real_send(*args)

        offboard0.set_position_velocity_acceleration_ned = counting_send
        await asyncio.sleep(0.3)
        assert sent_to_0, "sanity: drone 0 is being flown by playback before the abort"

        body = await gcs_main.abort_drone_land(0, operator=COMMANDER)
        assert body["status"] == "success"
        # Drone 0's column at (0,0) from 2m clears drone 1 (2m over, 4m up) by ~2.8m.
        assert body["clearance"]["clear"] is True, body["clearance"]
        sent_to_0.clear()

        # 2m at the sim's 1 m/s, plus touchdown settling before it disarms.
        await _wait_for(lambda: not manager.telemetry[0]["armed"], timeout=10.0)

        # Drone 0: landed, disarmed, never commanded by playback again.
        assert sent_to_0 == []
        assert manager.telemetry[0]["armed"] is False
        assert manager.telemetry[0]["fleet_z"] < 0.2
        assert manager.telemetry[0]["aborted"]["type"] == "land"
        assert manager.telemetry[0]["planned_valid"] is False

        # The rest of the fleet: still armed, still being flown, on plan.
        assert manager.is_running_trajectory is True
        assert manager.is_recording_telemetry is True
        for d_id in range(1, n):
            tel = manager.telemetry[d_id]
            assert tel["armed"] is True and tel["aborted"] is None
            assert tel["planned_valid"] is True
            deviation = ((tel["fleet_x"] - tel["planned_x"]) ** 2
                         + (tel["fleet_y"] - tel["planned_y"]) ** 2
                         + (tel["fleet_z"] - tel["planned_z"]) ** 2) ** 0.5
            assert deviation < 0.5, f"drone {d_id} is {deviation:.2f}m off plan"

        assert not any(manager.proximity_breaches.values())
        status = await gcs_main.get_status()
        assert status["aborted"] == [0]
        assert status["armed"] == n - 1
        assert status["trajectory_running"] is True

        # ABORTED survives a default (straggler-pickup) launch...
        await manager.launch_sequence()
        await asyncio.sleep(0.3)
        assert manager.telemetry[0]["armed"] is False
        assert manager.telemetry[0]["aborted"] is not None
        # ...and clears only on an explicit relaunch of that drone.
        await manager.launch_sequence([0])
        await asyncio.sleep(0.3)
        assert manager.telemetry[0]["armed"] is True
        assert manager.telemetry[0]["aborted"] is None
    finally:
        await manager.shutdown()
