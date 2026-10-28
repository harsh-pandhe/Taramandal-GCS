"""Issue #52: the fleet lifecycle state machine and the API gating built on it.

The first half tests `FleetStateMachine` as pure logic (the transition table,
409 detail, audit log). The second half runs a real `DroneManager` on
`backend="sim"` and drives it through the route functions the way
test_status_endpoint.py does, because the observed transitions (armed,
climbing, at formation altitude, disarmed) are only as good as the telemetry
they read, and a mocked fleet would only prove the table is self-consistent.
"""

import asyncio
import json
import time
from collections import deque

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tara_gcs import main as gcs_main
from tara_gcs.auth import Operator, Role
from tara_gcs.drone_manager import DroneManager
from tara_gcs.fleet_fsm import (
    AIRBORNE_STATES,
    COMMAND_RULES,
    EMERGENCY_STATES,
    TRANSITIONS,
    Command,
    FleetStateMachine,
    IllegalTransitionError,
    LifecycleState as S,
    can_transition,
)
from tara_gcs.telemetry_codec import encode_slow_frame

# Issue #64: route handlers called directly take the operator that the auth
# dependency would otherwise inject.
COMMANDER = Operator("test-commander", Role.FLIGHT_COMMANDER)


ISSUE_CHAIN = [
    S.STAGED, S.PREARM, S.ARMED, S.TAKEOFF, S.FORMATION_HOLD, S.RUNNING,
    S.SHOW_COMPLETE, S.LANDING, S.DISARMED,
]


def _drive_to(fsm: FleetStateMachine, target: S) -> None:
    """Walk ``fsm`` to ``target`` along legal transitions only (BFS over the table)."""
    parents = {fsm.state: None}
    queue = deque([fsm.state])
    while queue:
        node = queue.popleft()
        if node is target:
            break
        for nxt in sorted(TRANSITIONS[node] | EMERGENCY_STATES, key=lambda s: s.value):
            if nxt not in parents:
                parents[nxt] = node
                queue.append(nxt)
    path = []
    node = target
    while node is not fsm.state:
        path.append(node)
        node = parents[node]
    for state in reversed(path):
        fsm.transition(state, operator="test", trigger="drive_to")


# --- The state machine on its own -----------------------------------------


def test_the_issue_chain_is_the_legal_happy_path():
    fsm = FleetStateMachine(audit_path=None)
    for state in ISSUE_CHAIN[1:]:
        fsm.transition(state, operator="test", trigger="walk")
    assert [h["to"] for h in fsm.history] == [s.value for s in ISSUE_CHAIN[1:]]


def test_every_command_target_is_reachable_from_every_state_it_is_allowed_in():
    """The command rules and the transition table can't drift apart: a command
    the gate lets through must never then hit a ValueError in transition()."""
    for command, rule in COMMAND_RULES.items():
        if rule.target is None:
            continue
        for state in rule.allowed_from:
            assert can_transition(state, rule.target), (command, state)


def test_starting_the_show_before_takeoff_completes_is_rejected_with_an_explanation():
    fsm = FleetStateMachine(audit_path=None)
    _drive_to(fsm, S.TAKEOFF)
    seq = fsm.seq

    with pytest.raises(IllegalTransitionError) as exc:
        fsm.apply(Command.START_TRAJECTORY, operator="alice", trigger="test")

    detail = exc.value.detail()
    assert detail["error"] == "illegal_transition"
    assert detail["command"] == "start_trajectory"
    assert detail["state"] == "TAKEOFF"
    assert detail["allowed_from"] == ["FORMATION_HOLD"]
    assert "takeoff not complete" in detail["message"]
    # Rejected means nothing moved.
    assert fsm.state is S.TAKEOFF
    assert fsm.seq == seq


@pytest.mark.parametrize("state", sorted(AIRBORNE_STATES | {S.PREARM}, key=lambda s: s.value))
def test_launching_while_already_launched_or_flying_is_rejected(state):
    fsm = FleetStateMachine(audit_path=None)
    _drive_to(fsm, state)
    with pytest.raises(IllegalTransitionError):
        fsm.apply(Command.LAUNCH, operator="alice", trigger="test")
    assert fsm.state is state


@pytest.mark.parametrize("state", list(S))
@pytest.mark.parametrize("command,target", [
    (Command.LAND, S.LANDING), (Command.RTL, S.RTL), (Command.ESTOP, S.ESTOP),
])
def test_emergency_commands_are_legal_from_every_state(state, command, target):
    fsm = FleetStateMachine(audit_path=None)
    _drive_to(fsm, state)
    fsm.apply(command, operator="alice", trigger="test")
    assert fsm.state is target


def test_uploading_a_trajectory_is_refused_only_while_one_is_playing():
    for state in S:
        fsm = FleetStateMachine(audit_path=None)
        _drive_to(fsm, state)
        if state is S.RUNNING:
            with pytest.raises(IllegalTransitionError):
                fsm.check(Command.UPLOAD_TRAJECTORY, operator="alice", trigger="test")
        else:
            fsm.check(Command.UPLOAD_TRAJECTORY, operator="alice", trigger="test")
        assert fsm.state is state


def test_transitions_and_rejections_are_audited_with_time_operator_and_trigger(tmp_path):
    audit_path = tmp_path / "audit" / "fleet_audit.jsonl"
    fsm = FleetStateMachine(audit_path=str(audit_path))
    before = time.time()

    fsm.apply(Command.LAUNCH, operator="alice", trigger="POST /api/launch")
    with pytest.raises(IllegalTransitionError):
        fsm.apply(Command.START_TRAJECTORY, operator="bob", trigger="POST /api/start-trajectory")

    accepted, rejected = [json.loads(line) for line in audit_path.read_text().splitlines()]
    assert accepted["accepted"] is True
    assert (accepted["from"], accepted["to"]) == ("STAGED", "PREARM")
    assert accepted["operator"] == "alice"
    assert accepted["trigger"] == "POST /api/launch"
    assert accepted["ts"] >= before
    assert accepted["time"].endswith("+00:00")  # ISO-8601, UTC

    assert rejected["accepted"] is False
    assert rejected["command"] == "start_trajectory"
    assert rejected["state"] == "PREARM"
    assert rejected["operator"] == "bob"
    assert "Allowed from: FORMATION_HOLD" in rejected["reason"]


def test_audit_context_is_merged_into_every_entry_and_loses_to_explicit_fields(tmp_path):
    """Issue #170: the fleet lifecycle trail is the one audit trail that
    predates the context-provider pattern command_audit/preflight already
    use, so it could still be read as evidence from the wrong backend/
    fidelity tier. Tests the mechanism directly (context merging, explicit
    fields winning, a failing context never losing the entry) - the real
    backend/fidelity wiring is covered in test_status_endpoint.py's
    test_fleet_lifecycle_audit_entries_carry_backend_and_fidelity, against a
    real DroneManager.
    """
    audit_path = tmp_path / "fleet_audit.jsonl"
    fsm = FleetStateMachine(audit_path=str(audit_path), context=lambda: {"backend": "sim", "extra": "x"})
    fsm.apply(Command.LAUNCH, operator="alice", trigger="test")
    (entry,) = [json.loads(line) for line in audit_path.read_text().splitlines()]
    assert entry["backend"] == "sim"
    assert entry["extra"] == "x"
    # accepted=True is set by transition() itself, after the context is
    # merged in - must not be clobbered by a context that happened to carry
    # the same key.
    assert entry["accepted"] is True


def test_a_failing_audit_context_never_loses_the_entry(tmp_path):
    audit_path = tmp_path / "fleet_audit.jsonl"
    def _broken():
        raise RuntimeError("context provider is down")
    fsm = FleetStateMachine(audit_path=str(audit_path), context=_broken)
    fsm.apply(Command.LAUNCH, operator="alice", trigger="test")
    (entry,) = [json.loads(line) for line in audit_path.read_text().splitlines()]
    assert entry["accepted"] is True
    assert entry["operator"] == "alice"


def test_an_unwritable_audit_log_never_blocks_an_emergency(tmp_path):
    """A directory where the file should be makes every write fail."""
    fsm = FleetStateMachine(audit_path=str(tmp_path))
    fsm.apply(Command.RTL, operator="alice", trigger="test")
    assert fsm.state is S.RTL


def test_advance_does_nothing_once_the_state_has_moved_on():
    fsm = FleetStateMachine(audit_path=None)
    _drive_to(fsm, S.RUNNING)
    fsm.apply(Command.RTL, operator="alice", trigger="test")

    assert fsm.advance(S.RUNNING, S.SHOW_COMPLETE, trigger="trajectory_complete") is False
    assert fsm.state is S.RTL


def test_slow_frame_carries_the_fleet_state_when_given():
    fsm = FleetStateMachine(audit_path=None)
    telemetry = {0: {"battery_percent": 90.0}}

    assert "fleet_state" not in encode_slow_frame(telemetry)
    frame = encode_slow_frame(telemetry, fsm.snapshot())
    assert frame["fleet_state"]["state"] == "STAGED"
    assert "launch" in frame["fleet_state"]["allowed_commands"]
    json.dumps(frame)  # it goes over the socket as JSON


# --- Against a real sim fleet ---------------------------------------------


@pytest.fixture
def fleet(monkeypatch, tmp_path):
    """Install a sim-backend DroneManager as the API's module-level singleton."""
    managers = []

    def _make(n: int) -> DroneManager:
        manager = DroneManager(
            ports=list(range(n)),
            backend="sim",
            state_path=str(tmp_path / "show_state.json"),
            audit_path=str(tmp_path / "fleet_audit.jsonl"),
        )
        monkeypatch.setattr(gcs_main, "drone_manager", manager)
        managers.append(manager)
        return manager

    return _make


async def _connect(manager: DroneManager, timeout: float = 5.0) -> None:
    """connect_all(), then wait for every connection monitor to report in.

    A fixed sleep is a race: _launch_one skips any drone whose telemetry still
    says disconnected, which turns a slow first tick into "launch failed".
    """
    await manager.connect_all()
    deadline = time.monotonic() + timeout
    while not all(tel["connected"] and tel.get("health_armable") for tel in manager.telemetry.values()):
        assert time.monotonic() < deadline, "sim fleet never reported connected and armable"
        await asyncio.sleep(0.02)


async def _wait_for_state(manager: DroneManager, state: S, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while manager.fleet_fsm.state is not state:
        assert time.monotonic() < deadline, f"stuck in {manager.fleet_fsm.state.value}, expected {state.value}"
        await asyncio.sleep(0.05)


def _short_show(duration: float = 1.0) -> dict:
    # Hover in place at drone 0's launch altitude (2m), so the show itself
    # doesn't move anything the geofence or proximity monitors would react to.
    return {0: [
        {"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
        {"time": duration, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
    ]}


@pytest.mark.asyncio
async def test_a_whole_show_walks_the_lifecycle_and_illegal_commands_are_409(fleet):
    manager = fleet(1)
    try:
        await _connect(manager)

        await gcs_main.launch_fleet(operator=Operator("alice", Role.FLIGHT_COMMANDER))

        # Before takeoff completes: a start is a conflict, and so is launching again.
        with pytest.raises(HTTPException) as exc:
            await gcs_main.start_trajectory(gcs_main.StartTrajectoryRequest(), operator=COMMANDER)
        assert exc.value.status_code == 409
        assert exc.value.detail["command"] == "start_trajectory"

        await _wait_for_state(manager, S.FORMATION_HOLD)
        with pytest.raises(HTTPException) as exc:
            await gcs_main.launch_fleet(operator=COMMANDER)
        assert exc.value.status_code == 409
        assert exc.value.detail["state"] == "FORMATION_HOLD"

        manager.active_trajectory = _short_show()
        await gcs_main.start_trajectory(gcs_main.StartTrajectoryRequest(execution="streamed"), operator=Operator("alice", Role.FLIGHT_COMMANDER))
        await _wait_for_state(manager, S.SHOW_COMPLETE)

        with pytest.raises(HTTPException) as exc:
            await gcs_main.stop_trajectory(operator=COMMANDER)
        assert exc.value.status_code == 409

        await gcs_main.trigger_land(operator=Operator("alice", Role.FLIGHT_COMMANDER))
        await _wait_for_state(manager, S.DISARMED)

        history = [(h["from"], h["to"], h["operator"], h["trigger"]) for h in manager.fleet_fsm.history]
        assert history == [
            ("STAGED", "PREARM", "alice", "POST /api/launch"),
            ("PREARM", "ARMED", "system", "telemetry:first_drone_armed"),
            ("ARMED", "TAKEOFF", "system", "telemetry:climbing"),
            ("TAKEOFF", "FORMATION_HOLD", "system", "telemetry:formation_altitude_reached"),
            ("FORMATION_HOLD", "RUNNING", "alice", "POST /api/start-trajectory"),
            ("RUNNING", "SHOW_COMPLETE", "system", "trajectory_complete"),
            ("SHOW_COMPLETE", "LANDING", "alice", "POST /api/land"),
            ("LANDING", "DISARMED", "system", "telemetry:all_disarmed"),
        ]
        # Takeoff really had completed when the machine said so.
        assert manager.telemetry[0]["armed"] is False
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_a_failed_start_leaves_the_fleet_in_formation_hold(fleet):
    """No trajectory loaded is still the 400 it always was, and must not leave
    the machine claiming RUNNING with nothing running."""
    manager = fleet(1)
    _drive_to(manager.fleet_fsm, S.FORMATION_HOLD)

    with pytest.raises(HTTPException) as exc:
        await gcs_main.start_trajectory(gcs_main.StartTrajectoryRequest(), operator=COMMANDER)

    assert exc.value.status_code == 400
    assert manager.fleet_fsm.state is S.FORMATION_HOLD


@pytest.mark.asyncio
async def test_an_emergency_during_launch_stops_the_rest_of_the_launch(fleet):
    manager = fleet(2)
    try:
        await _connect(manager)  # connected, but still inside the sim's 0.3s armable delay

        await gcs_main.launch_fleet(operator=COMMANDER)
        await gcs_main.trigger_land(operator=COMMANDER)
        await manager.launch_task

        assert not any(tel["armed"] for tel in manager.telemetry.values())
        await _wait_for_state(manager, S.DISARMED, timeout=5.0)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_estop_kills_the_motors_and_settles_to_disarmed(fleet):
    manager = fleet(1)
    try:
        await _connect(manager)
        await gcs_main.launch_fleet(operator=COMMANDER)
        await _wait_for_state(manager, S.FORMATION_HOLD)

        await gcs_main.trigger_estop(operator=Operator("alice", Role.FLIGHT_COMMANDER))
        assert manager.fleet_fsm.state is S.ESTOP

        await _wait_for_state(manager, S.DISARMED, timeout=5.0)
        assert manager.telemetry[0]["armed"] is False
        assert manager.telemetry[0]["fleet_z"] == pytest.approx(0.0, abs=0.1)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_a_failsafe_rtl_is_audited_as_the_system(fleet):
    manager = fleet(1)
    manager.telemetry = {0: {"connected": True, "armed": True, "local_x": 500.0, "local_y": 0.0, "local_z": 5.0}}
    _drive_to(manager.fleet_fsm, S.RUNNING)

    task = asyncio.create_task(manager._geofence_monitor_loop())
    try:
        await asyncio.sleep(0.15)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    last = manager.fleet_fsm.history[-1]
    assert (last["from"], last["to"]) == ("RUNNING", "RTL")
    assert (last["operator"], last["trigger"]) == ("system", "failsafe:geofence")


@pytest.mark.asyncio
async def test_a_playback_error_returns_the_fleet_to_formation_hold(fleet):
    manager = fleet(1)
    _drive_to(manager.fleet_fsm, S.RUNNING)
    manager.drones = {0: object()}  # a drone with no telemetry entry: KeyError mid-tick
    manager.is_running_trajectory = True

    with pytest.raises(KeyError):
        await manager._run_trajectory_loop(_short_show())

    last = manager.fleet_fsm.history[-1]
    assert (last["to"], last["trigger"]) == ("FORMATION_HOLD", "trajectory_error")


@pytest.mark.asyncio
async def test_a_fleet_that_disarms_mid_show_ends_the_show_but_a_link_loss_does_not(fleet):
    manager = fleet(1)
    _drive_to(manager.fleet_fsm, S.RUNNING)
    manager.start_trajectory(_short_show(duration=30.0), execution="streamed")
    try:
        # Link lost: last-known armed is still True, so nothing changes.
        manager.telemetry = {0: {"connected": False, "armed": True, "fleet_z": 5.0}}
        manager.evaluate_lifecycle()
        assert manager.fleet_fsm.state is S.RUNNING
        assert manager.is_running_trajectory is True

        manager.telemetry = {0: {"connected": True, "armed": False, "fleet_z": 0.0}}
        manager.evaluate_lifecycle()
        assert manager.fleet_fsm.state is S.DISARMED
        assert manager.is_running_trajectory is False
    finally:
        manager.stop_trajectory()


@pytest.mark.asyncio
async def test_a_launch_that_arms_nobody_returns_to_staged(fleet):
    manager = fleet(1)
    try:
        await _connect(manager)

        async def _failing_arm():
            raise RuntimeError("simulated arming failure")

        manager.drones[0].action.arm = _failing_arm

        await gcs_main.launch_fleet(operator=COMMANDER)
        await manager.launch_task
        manager.evaluate_lifecycle()

        last = manager.fleet_fsm.history[-1]
        assert (last["from"], last["to"], last["trigger"]) == ("PREARM", "STAGED", "launch_failed:no_drone_armed")
    finally:
        await manager.shutdown()


# --- Over HTTP and the telemetry socket -----------------------------------


def test_an_illegal_command_is_an_http_409_with_a_descriptive_body(fleet):
    """TestClient without `with` skips the startup hook, so nothing connects."""
    fleet(1)
    client = TestClient(gcs_main.app)

    response = client.post(
        "/api/start-trajectory",
        json={"mode": "LOCAL", "trigger_epoch_ms": 0},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["state"] == "STAGED"
    assert detail["allowed_from"] == ["FORMATION_HOLD"]
    assert "Cannot start the show trajectory while the fleet is STAGED" in detail["message"]

    state = client.get("/api/fleet-state").json()
    assert state["state"] == "STAGED"
    assert "launch" in state["allowed_commands"]
    assert "start_trajectory" not in state["allowed_commands"]


def test_a_transition_reaches_the_hud_without_waiting_for_the_1hz_slow_frame(fleet):
    manager = fleet(1)
    client = TestClient(gcs_main.app)

    with client.websocket_connect("/ws/telemetry") as ws:
        def next_slow_frame() -> dict:
            while True:
                message = ws.receive()
                if message.get("text"):
                    return json.loads(message["text"])

        assert next_slow_frame()["fleet_state"]["state"] == "STAGED"

        sent_at = time.monotonic()
        manager.fleet_fsm.apply(Command.RTL, operator="alice", trigger="test")
        frame = next_slow_frame()

        assert frame["fleet_state"]["state"] == "RTL"
        assert frame["fleet_state"]["last_transition"]["operator"] == "alice"
        # The next scheduled slow frame is ~1s away; this one was pushed.
        assert time.monotonic() - sent_at < 0.9
