"""Fleet-wide LAND / RTL / E-STOP when a vehicle refuses the command (issue #171).

PX4 refuses ``land`` once the vehicle is in flight termination, and the per-drone
routes already answer that with a 502. The fleet-wide routes used to drop every
drone's result: ``POST /api/land`` answered 200 "success" and the fleet lifecycle
moved to LANDING although PX4 had said COMMAND_DENIED (observed in
docs/sitl-171-fault-rows/denied-land-3). RTL and E-STOP had the same shape.

What these tests hold the fix to:

* any refusal is a 502 carrying every drone's outcome;
* the command still reaches every drone, however many refuse or raise;
* the fleet lifecycle does not move when EVERY drone refused, and does move
  when at least one accepted (decision recorded in the PR: those drones are
  already landing / returning / killed);
* the refusal is audited with the drone ids and the vehicle's own words;
* an E-STOP is sent to every drone before anything else waits on it.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from tara_gcs import main as gcs_main
from tara_gcs.auth import Operator, Role
from tara_gcs.drone_manager import DroneManager, FleetCommandResult
from tara_gcs.fleet_fsm import LifecycleState as S

COMMANDER = Operator("test-commander", Role.FLIGHT_COMMANDER)

#: route name, DroneManager trigger, per-drone MAVSDK action, state the command moves the fleet to
CASES = [
    ("trigger_land", "trigger_land", "land", S.LANDING),
    ("trigger_rtl", "trigger_rtl", "return_to_launch", S.RTL),
    ("trigger_estop", "trigger_estop", "kill", S.ESTOP),
]
CASE_IDS = [c[0] for c in CASES]


@pytest.fixture
def fleet(monkeypatch, tmp_path):
    """Three mock px4 drones behind the module-level manager the routes use."""
    manager = DroneManager(
        ports=[],
        state_path=str(tmp_path / "show_state.json"),
        audit_path=str(tmp_path / "fleet_audit.jsonl"),
    )
    manager.drones = {i: AsyncMock() for i in range(3)}
    manager.stop_trajectory = lambda: None
    # The fleet is mid-show: the state an emergency command most needs to be honest about.
    for target in (S.PREARM, S.ARMED, S.TAKEOFF, S.FORMATION_HOLD, S.RUNNING):
        manager.fleet_fsm.transition(target, operator="test", trigger="setup")
    monkeypatch.setattr(gcs_main, "drone_manager", manager)
    return manager


def _audit(manager) -> list[dict]:
    with open(manager.fleet_fsm.audit_path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _refuse(manager, drone_id: int, action: str, text: str) -> None:
    getattr(manager.drones[drone_id].action, action).side_effect = Exception(text)


async def _call(route: str):
    return await getattr(gcs_main, route)(operator=COMMANDER)


@pytest.mark.parametrize("route, trigger, action, target", CASES, ids=CASE_IDS)
async def test_every_drone_refusing_is_a_502_and_the_fleet_state_does_not_move(fleet, route, trigger, action, target):
    for i in range(3):
        _refuse(fleet, i, action, f"COMMAND_DENIED: drone {i}")

    with pytest.raises(HTTPException) as exc:
        await _call(route)

    assert exc.value.status_code == 502
    detail = exc.value.detail
    assert detail["accepted_ids"] == [] and detail["refused_ids"] == [0, 1, 2]
    assert detail["drones"] == {
        str(i): {"accepted": False, "error": f"COMMAND_DENIED: drone {i}"} for i in range(3)
    }
    assert detail["fleet_state"] == "RUNNING" and detail["fleet_state_advanced"] is False
    assert "Nothing was commanded" in detail["message"]
    # No lie in the lifecycle, and none in the audit's transition history.
    assert fleet.fleet_fsm.state is S.RUNNING
    assert all(h["to"] != target.value for h in fleet.fleet_fsm.history)
    # The command was still sent to every drone.
    for drone in fleet.drones.values():
        getattr(drone.action, action).assert_awaited_once()


@pytest.mark.parametrize("route, trigger, action, target", CASES, ids=CASE_IDS)
async def test_drone_0_refusing_still_commands_drones_1_and_2(fleet, route, trigger, action, target):
    _refuse(fleet, 0, action, "COMMAND_DENIED: Command Denied")

    with pytest.raises(HTTPException) as exc:
        await _call(route)

    assert exc.value.status_code == 502
    detail = exc.value.detail
    assert detail["accepted_ids"] == [1, 2] and detail["refused_ids"] == [0]
    assert detail["drones"]["0"] == {"accepted": False, "error": "COMMAND_DENIED: Command Denied"}
    assert detail["drones"]["1"] == {"accepted": True, "error": None}
    assert detail["drones"]["2"] == {"accepted": True, "error": None}
    assert "drone 0: COMMAND_DENIED: Command Denied" in detail["message"]
    # The refusal did not stop the command reaching the others (all three were sent it).
    for drone in fleet.drones.values():
        getattr(drone.action, action).assert_awaited_once()
    # Partial: drones 1 and 2 accepted, so the fleet is no longer in its old state.
    assert detail["fleet_state"] == target.value and detail["fleet_state_advanced"] is True
    assert fleet.fleet_fsm.state is target


@pytest.mark.parametrize("route, trigger, action, target", CASES, ids=CASE_IDS)
async def test_partial_refusal_is_audited_with_the_transition_and_the_refusal(fleet, route, trigger, action, target):
    _refuse(fleet, 0, action, "COMMAND_DENIED: Command Denied")

    with pytest.raises(HTTPException):
        await _call(route)

    entries = _audit(fleet)
    transition = [e for e in entries if e.get("accepted") and e.get("to") == target.value]
    refusal = [e for e in entries if e.get("accepted") is False and e.get("refused_drones")]
    assert len(transition) == 1 and transition[0]["trigger"] == f"POST /api/{route.removeprefix('trigger_')}"
    assert len(refusal) == 1
    assert refusal[0]["refused_drones"] == {"0": "COMMAND_DENIED: Command Denied"}
    assert refusal[0]["accepted_drones"] == [1, 2]
    assert refusal[0]["fleet_state_advanced"] is True
    assert refusal[0]["operator"] == COMMANDER.id


@pytest.mark.parametrize("route, trigger, action, target", CASES, ids=CASE_IDS)
async def test_total_refusal_is_audited_as_a_refusal_not_a_transition(fleet, route, trigger, action, target):
    for i in range(3):
        _refuse(fleet, i, action, "COMMAND_DENIED")

    with pytest.raises(HTTPException):
        await _call(route)

    entries = _audit(fleet)
    assert not [e for e in entries if e.get("accepted") and e.get("to") == target.value]
    refusal = [e for e in entries if e.get("accepted") is False and e.get("refused_drones")]
    assert len(refusal) == 1
    assert sorted(refusal[0]["refused_drones"]) == ["0", "1", "2"]
    assert refusal[0]["accepted_drones"] == [] and refusal[0]["fleet_state_advanced"] is False
    assert refusal[0]["state"] == "RUNNING"


@pytest.mark.parametrize("route, trigger, action, target", CASES, ids=CASE_IDS)
async def test_all_accepting_is_still_a_200_body_and_moves_the_fleet(fleet, route, trigger, action, target):
    res = await _call(route)

    assert res["status"] == "success"
    assert res["accepted_ids"] == [0, 1, 2]
    assert fleet.fleet_fsm.state is target
    assert not [e for e in _audit(fleet) if e.get("refused_drones")]


@pytest.mark.parametrize("route, trigger, action, target", CASES, ids=CASE_IDS)
async def test_a_primitive_that_raises_counts_as_a_refusal_and_does_not_stop_the_others(
    fleet, monkeypatch, route, trigger, action, target,
):
    """A drone that vanishes mid-command (the primitives index ``self.drones``) must not take the fleet command with it."""
    primitive = {"land": "_land_one", "return_to_launch": "_rtl_one", "kill": "_estop_one"}[action]
    real = getattr(fleet, primitive)

    async def flaky(drone_id):
        if drone_id == 0:
            raise RuntimeError("link dropped")
        return await real(drone_id)

    monkeypatch.setattr(fleet, primitive, flaky)

    with pytest.raises(HTTPException) as exc:
        await _call(route)

    assert exc.value.status_code == 502
    assert exc.value.detail["drones"]["0"] == {"accepted": False, "error": "link dropped"}
    assert exc.value.detail["accepted_ids"] == [1, 2]
    for i in (1, 2):
        getattr(fleet.drones[i].action, action).assert_awaited_once()


@pytest.mark.parametrize("route, trigger, action, target", CASES, ids=CASE_IDS)
async def test_no_connected_drones_is_not_a_refusal(fleet, route, trigger, action, target):
    """Unchanged behaviour: with nothing to command the emergency is still recorded."""
    fleet.drones = {}

    res = await _call(route)

    assert res["status"] == "success" and res["accepted_ids"] == []
    assert fleet.fleet_fsm.state is target


@pytest.mark.parametrize("route, trigger, action, target", CASES, ids=CASE_IDS)
async def test_internal_callers_get_the_result_and_never_an_exception(fleet, route, trigger, action, target):
    """The proximity and geofence failsafes call trigger_rtl directly and ignore its value."""
    for i in range(3):
        _refuse(fleet, i, action, "COMMAND_DENIED")

    result = await getattr(fleet, trigger)(trigger="failsafe:test")

    assert isinstance(result, FleetCommandResult)
    assert result.all_refused and result.refused_ids == [0, 1, 2]
    assert fleet.fleet_fsm.state is S.RUNNING


async def test_estop_reaches_every_drone_while_a_slow_one_is_still_pending(fleet):
    """E-STOP is not serialised behind a drone that is slow to answer, nor behind the lifecycle."""
    release = asyncio.Event()

    async def slow_kill():
        await release.wait()

    fleet.drones[0].action.kill.side_effect = slow_kill

    task = asyncio.create_task(fleet.trigger_estop(operator="test", trigger="POST /api/estop"))
    for _ in range(20):
        await asyncio.sleep(0)

    assert not task.done()
    fleet.drones[1].action.kill.assert_awaited_once()
    fleet.drones[2].action.kill.assert_awaited_once()

    release.set()
    result = await task
    assert result.accepted_ids == [0, 1, 2]
    assert fleet.fleet_fsm.state is S.ESTOP


async def test_estop_refused_by_one_drone_does_not_delay_the_accepting_drones(fleet):
    """Drone 0 refuses at once; the kill for drones 1 and 2 is already in flight, in the same event-loop turn."""
    _refuse(fleet, 0, "kill", "COMMAND_DENIED")
    calls: list[int] = []
    for i in (1, 2):
        fleet.drones[i].action.kill.side_effect = lambda i=i: calls.append(i)

    with pytest.raises(HTTPException) as exc:
        await _call("trigger_estop")

    assert sorted(calls) == [1, 2]
    assert exc.value.detail["accepted_ids"] == [1, 2]
    assert fleet.fleet_fsm.state is S.ESTOP  # partial: two motors were killed


def test_fleet_command_result_views():
    result = FleetCommandResult("land", {2: None, 0: "denied", 1: None})
    assert result.accepted_ids == [1, 2] and result.refused_ids == [0]
    assert result.refused and not result.all_refused
    assert result.breakdown()["0"] == {"accepted": False, "error": "denied"}
    assert not FleetCommandResult("land", {}).refused
    assert not FleetCommandResult("land", {}).all_refused
    assert FleetCommandResult("land", {0: "x"}).all_refused
