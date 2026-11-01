"""A streamed start flies the transition from the formation hold into the show,
instead of stepping every drone to the show's first waypoint (issue #172, F3).

Driven through the real API routes on a sim fleet that has actually launched, so
the hold gate, the lifecycle and the position telemetry are the production
ones. The fleet is left hovering at 2, 4, 6 m (``_hover_altitude``) and the show
starts on the pad, exactly as the Studio's sequencer builds it.
"""

import asyncio
import time

import pytest
from fastapi import HTTPException

from tara_core.geometry import interpolate_waypoint
from tara_gcs.fleet_fsm import LifecycleState as S
from tara_gcs import main as gcs_main
from tara_gcs.auth import Operator, Role
from tara_gcs.drone_manager import DroneManager, _pad_offset
from tara_gcs.start_bridge import BRIDGE_PEAK_SPEED_MS

COMMANDER = Operator("test-commander", Role.FLIGHT_COMMANDER)
SAMPLE_S = 0.02


def _show_from_pad(n: int) -> dict:
    """What the Studio sequencer emits: on the pad at t=0, then up to 3 m."""
    show = {}
    for i in range(n):
        x, y = _pad_offset(i)
        show[i] = [
            {"time": 0.0, "x": x, "y": y, "z": 0.0, "yaw": 0.0},
            {"time": 3.0, "x": x, "y": y, "z": -3.0, "yaw": 0.0},
            {"time": 5.0, "x": x, "y": y, "z": -3.0, "yaw": 0.0},
        ]
    return show


@pytest.fixture
def fleet(monkeypatch, tmp_path):
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


async def _hold_formation(manager: DroneManager) -> None:
    """connect, launch, and wait for the gate's FORMATION_HOLD."""
    await manager.connect_all()
    deadline = time.monotonic() + 5.0
    while not all(t["connected"] and t.get("health_armable") for t in manager.telemetry.values()):
        assert time.monotonic() < deadline, "sim fleet never reported connected and armable"
        await asyncio.sleep(0.02)
    await gcs_main.launch_fleet(operator=COMMANDER)
    deadline = time.monotonic() + 30.0
    while manager.fleet_fsm.state is not S.FORMATION_HOLD:
        assert time.monotonic() < deadline, f"stuck in {manager.fleet_fsm.state.value}"
        await asyncio.sleep(0.05)


async def _start(show: dict) -> dict:
    gcs_main.drone_manager.active_trajectory = show
    return await gcs_main.start_trajectory(gcs_main.StartTrajectoryRequest(execution="streamed"), operator=COMMANDER)


async def _sample_planned_altitude(manager: DroneManager, seconds: float) -> dict[int, list[float]]:
    """The altitude the loop is commanding, polled much faster than its 10 Hz
    tick, distinct values only."""
    # Seeded with where each drone is hovering: the jump this guards against
    # is from there to the first command, which a series of commands alone hides.
    seen: dict[int, list[float]] = {i: [tel["fleet_z"]] for i, tel in manager.telemetry.items()}
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for i, tel in manager.telemetry.items():
            z = tel.get("planned_z")
            if tel.get("planned_valid") and z != seen[i][-1]:
                seen[i].append(z)
        await asyncio.sleep(SAMPLE_S)
    return seen


@pytest.mark.asyncio
async def test_the_first_commands_are_the_hold_position_not_the_pad(fleet):
    manager = fleet(3)
    try:
        await _hold_formation(manager)
        hold_alt = {i: manager.telemetry[i]["fleet_z"] for i in range(3)}
        assert hold_alt[2] > 4.0  # the highest layer is the one that used to dive furthest

        await _start(_show_from_pad(3))
        planned = await _sample_planned_altitude(manager, 1.2)

        for i in range(3):
            assert planned[i][1] == pytest.approx(hold_alt[i], abs=0.3), f"drone {i} first command"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_the_commanded_altitude_never_changes_faster_than_the_leg_allows(fleet):
    """The step this replaces was the whole hold altitude in one 10 Hz tick."""
    manager = fleet(3)
    try:
        await _hold_formation(manager)
        await _start(_show_from_pad(3))
        planned = await _sample_planned_altitude(manager, 2.0)

        limit_per_tick = BRIDGE_PEAK_SPEED_MS * 0.1 * 1.1
        for i, values in planned.items():
            steps = [abs(b - a) for a, b in zip(values, values[1:])]
            assert len(steps) > 3, f"drone {i}: no commands seen"
            assert max(steps) <= limit_per_tick, f"drone {i} stepped {max(steps):.2f} m in one tick"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_the_response_and_manager_report_how_late_the_show_starts(fleet):
    manager = fleet(3)
    try:
        await _hold_formation(manager)

        response = await _start(_show_from_pad(3))

        assert response["bridge_s"] == manager.start_bridge_s
        assert response["bridge_s"] > 1.0
        for i in range(3):
            assert manager.playing_trajectory[i][-1]["time"] == pytest.approx(5.0 + response["bridge_s"])
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_the_loaded_show_is_not_rewritten_so_resume_and_upload_still_see_the_original(fleet):
    manager = fleet(3)
    try:
        await _hold_formation(manager)
        show = _show_from_pad(3)

        await _start(show)

        assert manager.active_trajectory is show
        assert show[0][0]["time"] == 0.0 and len(show[0]) == 3
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_a_show_that_already_starts_at_the_hold_is_streamed_exactly_as_before(fleet):
    manager = fleet(2)
    try:
        await _hold_formation(manager)
        show = {}
        for i in range(2):
            x, y = _pad_offset(i)
            alt = manager.telemetry[i]["fleet_z"]
            show[i] = [
                {"time": 0.0, "x": x, "y": y, "z": -alt, "yaw": 0.0},
                {"time": 2.0, "x": x + 1.0, "y": y, "z": -alt, "yaw": 0.0},
            ]

        response = await _start(show)

        assert response["bridge_s"] == 0.0
        assert manager.playing_trajectory == show
    finally:
        await manager.shutdown()


# --- the gate is still the gate ------------------------------------------------


@pytest.mark.asyncio
async def test_a_transition_that_would_breach_separation_is_refused_and_nothing_starts(fleet):
    """Drones 0 and 1 are asked to swap places: their legs run along the same
    line in opposite directions and meet in the middle."""
    manager = fleet(2)
    try:
        await _hold_formation(manager)
        here = {i: (manager.telemetry[i]["fleet_x"], manager.telemetry[i]["fleet_y"], -manager.telemetry[i]["fleet_z"]) for i in range(2)}
        swapped = {
            0: [{"time": 0.0, "x": here[1][0], "y": here[1][1], "z": here[1][2], "yaw": 0.0}],
            1: [{"time": 0.0, "x": here[0][0], "y": here[0][1], "z": here[0][2], "yaw": 0.0}],
        }

        with pytest.raises(HTTPException) as refused:
            await _start(swapped)

        assert refused.value.status_code == 400
        assert "Start refused" in refused.value.detail
        assert manager.fleet_fsm.state is S.FORMATION_HOLD
        assert not manager.is_running_trajectory
        assert manager.playing_trajectory is None
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_a_start_outside_formation_hold_is_still_refused_before_any_leg_is_planned(fleet):
    manager = fleet(2)
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)
        assert manager.fleet_fsm.state is S.STAGED

        with pytest.raises(HTTPException) as refused:
            await _start(_show_from_pad(2))

        assert refused.value.status_code == 409
        assert not manager.is_running_trajectory
        assert manager.playing_trajectory is None
        assert manager.start_bridge_s == 0.0
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_stopping_during_the_leg_ends_the_show_and_returns_to_formation_hold(fleet):
    manager = fleet(3)
    try:
        await _hold_formation(manager)
        await _start(_show_from_pad(3))
        await asyncio.sleep(0.5)  # inside the leg

        await gcs_main.stop_trajectory(operator=COMMANDER)
        await asyncio.sleep(0.3)  # the cancelled loop clears its flags on its next turn

        assert manager.fleet_fsm.state is S.FORMATION_HOLD
        assert not manager.is_running_trajectory
        assert not any(t.get("planned_valid") for t in manager.telemetry.values())
        # And the drones are where the leg had taken them, still armed, not dropped.
        assert all(t["armed"] for t in manager.telemetry.values())
        assert all(t["fleet_z"] > 1.0 for t in manager.telemetry.values())
    finally:
        await manager.shutdown()
