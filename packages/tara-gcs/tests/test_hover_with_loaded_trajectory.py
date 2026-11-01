"""A loaded-but-unstarted show must not block the single-drone hover (issue #171).

Seen on real PX4 SITL: after a trajectory upload, the hover was refused with
"Cannot run the single-drone sequence while a fleet show is active" until the
GCS restarted, although nothing was flying. active_trajectory stays set from
upload onward, so it says a show is loaded, not that one is active.

That check also does a job which has to survive: after a GCS restart mid-show
the FSM reads STAGED while drones are airborne, and /api/resume-show sets
active_trajectory. So a loaded show still refuses the hover unless the whole
fleet is confirmed parked from fresh telemetry.
"""

import asyncio
import dataclasses
import time

import pytest
from fastapi import HTTPException

from tara_gcs import main as gcs_main
from tara_gcs.auth import Operator, Role
from tara_gcs.drone_manager import DroneManager
from tara_gcs.fleet_fsm import LifecycleState


def _show(n: int = 1) -> dict:
    return {
        i: [
            {"time": 0.0, "x": 0.0, "y": 3.0 * i, "z": 2.0},
            {"time": 5.0, "x": 1.0, "y": 3.0 * i, "z": 2.0},
        ]
        for i in range(n)
    }


@pytest.fixture
def commander():
    return Operator(id="test-commander", role=Role.FLIGHT_COMMANDER)


@pytest.fixture
async def make_manager(monkeypatch):
    managers = []

    async def _make(n: int = 1) -> DroneManager:
        manager = DroneManager(ports=[14540 + i for i in range(n)], backend="sim")
        monkeypatch.setattr(gcs_main, "drone_manager", manager)
        managers.append(manager)
        await manager.connect_all()
        # landed_state comes off the noisy position, so a parked sim drone reads
        # IN_AIR in about 0.6% of samples (0.05 m threshold, 0.02 m sigma). That
        # refusal is correct but random; these tests are about the rule.
        state = manager._sim_fleet.state
        state.config = dataclasses.replace(state.config, position_noise_std_m=0.0)
        # Clears tara_sim's DEFAULT_ARMABLE_DELAY_S so the pre-arm gates go green.
        await asyncio.sleep(0.5)
        return manager

    yield _make
    for manager in managers:
        await manager.shutdown()


async def _hover(commander):
    return await gcs_main.run_single_drone_sequence(
        gcs_main.SingleDroneSequenceRequest(target_altitude_m=0.3, hover_duration_s=1.0),
        operator=commander,
    )


async def _assert_refused(commander, *expected_in_detail):
    with pytest.raises(HTTPException) as exc:
        await _hover(commander)
    assert exc.value.status_code == 409
    for text in expected_in_detail:
        assert text in exc.value.detail
    return exc.value.detail


def _freeze_telemetry(manager, monkeypatch, drone_id, **fields):
    """Pin telemetry fields against the sim's own ingest, which runs every tick."""
    original = manager._ingest_bulk_telemetry

    def ingest(snap, include_slow):
        original(snap, include_slow)
        manager.telemetry[drone_id].update(fields)

    monkeypatch.setattr(manager, "_ingest_bulk_telemetry", ingest)
    manager.telemetry[drone_id].update(fields)


async def test_hover_runs_with_a_loaded_unstarted_show(commander, make_manager):
    """The #171 finding: this used to be a 409 until the GCS restarted."""
    manager = await make_manager()
    manager.active_trajectory = _show()

    result = await _hover(commander)

    assert result["status"] == "COMPLETED"
    assert manager.telemetry[0]["armed"] is False


async def test_hover_leaves_the_loaded_show_and_the_fsm_alone(commander, make_manager):
    manager = await make_manager()
    show = _show()
    manager.active_trajectory = show

    await _hover(commander)

    assert manager.active_trajectory is show
    assert manager.fleet_fsm.state is LifecycleState.STAGED
    assert manager.is_running_trajectory is False


async def test_hover_runs_with_a_finished_shows_trajectory_still_loaded(commander, make_manager):
    """active_trajectory also outlives a show that has ended."""
    manager = await make_manager()
    manager.active_trajectory = _show()
    manager.stop_trajectory()

    assert (await _hover(commander))["status"] == "COMPLETED"


async def test_running_show_still_refuses_the_hover(commander, make_manager):
    manager = await make_manager()
    manager.active_trajectory = _show()
    manager.is_running_trajectory = True
    try:
        await _assert_refused(commander, "fleet show is active")
    finally:
        manager.is_running_trajectory = False


async def test_loaded_show_with_an_armed_drone_refuses_the_hover(commander, make_manager, monkeypatch):
    """Restart mid-show, then /api/resume-show: FSM says STAGED, drones say otherwise."""
    manager = await make_manager(2)
    manager.active_trajectory = _show(2)
    assert manager.fleet_fsm.state is LifecycleState.STAGED
    _freeze_telemetry(manager, monkeypatch, 1, armed=True)

    await _assert_refused(commander, "fleet show is active", "Drone 1")


async def test_loaded_show_with_an_airborne_drone_refuses_the_hover(commander, make_manager, monkeypatch):
    """Airborne and disarmed-looking (armed flag not yet seen after a restart)."""
    manager = await make_manager(2)
    manager.active_trajectory = _show(2)
    _freeze_telemetry(manager, monkeypatch, 1, landed_state="IN_AIR")

    await _assert_refused(commander, "fleet show is active", "Drone 1")


async def test_loaded_show_with_unknown_landed_state_refuses_the_hover(commander, make_manager, monkeypatch):
    """Unknown is not parked: no 'unknown counts as landed' fallback."""
    manager = await make_manager(2)
    manager.active_trajectory = _show(2)
    _freeze_telemetry(manager, monkeypatch, 1, landed_state=None)

    await _assert_refused(commander, "fleet show is active", "Drone 1")


async def test_loaded_show_with_stale_landed_state_refuses_the_hover(commander, make_manager, monkeypatch):
    """A last-known ON_GROUND off a stalled stream is not a confirmation."""
    manager = await make_manager(2)
    manager.active_trajectory = _show(2)
    _freeze_telemetry(
        manager, monkeypatch, 1,
        landed_state="ON_GROUND",
        landed_state_at=time.time() - manager.HEARTBEAT_STALE_S - 1.0,
    )

    await _assert_refused(commander, "fleet show is active", "Drone 1")


async def test_loaded_show_with_a_silent_fleet_member_refuses_the_hover(commander, make_manager, monkeypatch):
    manager = await make_manager(2)
    manager.active_trajectory = _show(2)
    _freeze_telemetry(manager, monkeypatch, 1, connected=False)

    await _assert_refused(commander, "fleet show is active", "Drone 1")


async def test_no_show_loaded_does_not_ask_about_the_rest_of_the_fleet(commander, make_manager, monkeypatch):
    """Scope guard: a lone bench drone with no show keeps today's behaviour."""
    manager = await make_manager(2)
    assert manager.active_trajectory is None
    _freeze_telemetry(manager, monkeypatch, 1, landed_state=None)

    assert (await _hover(commander))["status"] == "COMPLETED"


async def test_start_trajectory_is_refused_during_the_hover(commander, make_manager):
    """A show can now be loaded while the hover flies, which used to be impossible."""
    manager = await make_manager()
    manager.active_trajectory = _show()

    hover = asyncio.create_task(_hover(commander))
    try:
        await asyncio.sleep(0.2)
        assert 0 in manager.single_drone_sequence_active
        with pytest.raises(HTTPException) as exc:
            await gcs_main.start_trajectory(gcs_main.StartTrajectoryRequest(), operator=commander)
        assert exc.value.status_code == 409
        assert manager.is_running_trajectory is False
    finally:
        await hover
