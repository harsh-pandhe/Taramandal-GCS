"""The fleet reads RUNNING but the show is not being played (issue #299).

Observed on real PX4 SITL (docs/sitl-airborne-denied-land): a fleet-wide LAND stops the show playback before the
vehicle answers. When PX4 refuses, the fleet state is (rightly) not advanced to LANDING, but nothing moves it out of
RUNNING either, so the state kept reading RUNNING for as long as it was watched with no show running. The state machine
is left as it is; ``playback_stopped`` is the derived fact "RUNNING and the playback loop is not running", exposed on
``/api/status``, ``/api/fleet-state``, the websocket slow frame and the refusal's 502 body.
"""

import json
import time
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tara_gcs import main as gcs_main
from tara_gcs.auth import Operator, Role
from tara_gcs.drone_manager import DroneManager
from tara_gcs.fleet_fsm import LifecycleState as S

COMMANDER = Operator("test-commander", Role.FLIGHT_COMMANDER)
TO_RUNNING = (S.PREARM, S.ARMED, S.TAKEOFF, S.FORMATION_HOLD, S.RUNNING)


@pytest.fixture
def playing_fleet(monkeypatch, tmp_path):
    """Three mock px4 drones, the fleet RUNNING and the playback loop on; stop_trajectory stops it as the real one does."""
    manager = DroneManager(
        ports=[],
        state_path=str(tmp_path / "show_state.json"),
        audit_path=str(tmp_path / "fleet_audit.jsonl"),
    )
    manager.drones = {i: AsyncMock() for i in range(3)}
    for target in TO_RUNNING:
        manager.fleet_fsm.transition(target, operator="test", trigger="setup")
    manager.is_running_trajectory = True

    def stop_trajectory():
        manager.is_running_trajectory = False

    manager.stop_trajectory = stop_trajectory
    monkeypatch.setattr(gcs_main, "drone_manager", manager)
    return manager


def _refuse(manager, drone_id: int) -> None:
    manager.drones[drone_id].action.land.side_effect = Exception("COMMAND_DENIED: 'Command Denied'")


@pytest.mark.parametrize(
    "state, playing, expected",
    [
        (S.RUNNING, True, False),   # a show that is playing
        (S.RUNNING, False, True),   # the reported case
        (S.FORMATION_HOLD, False, False),
        (S.SHOW_COMPLETE, False, False),
    ],
)
def test_playback_stopped_is_only_running_without_playback(playing_fleet, state, playing, expected):
    if state is not S.RUNNING:
        playing_fleet.fleet_fsm.transition(state, operator="test", trigger="setup")
    playing_fleet.is_running_trajectory = playing
    assert gcs_main._playback_stopped() is expected


@pytest.mark.asyncio
async def test_a_land_every_vehicle_refuses_mid_show_reports_playback_stopped(playing_fleet):
    for i in range(3):
        _refuse(playing_fleet, i)

    with pytest.raises(HTTPException) as exc:
        await gcs_main.trigger_land(operator=COMMANDER)

    detail = exc.value.detail
    assert exc.value.status_code == 502
    assert detail["fleet_state"] == "RUNNING" and detail["fleet_state_advanced"] is False
    assert detail["playback_stopped"] is True
    assert (await gcs_main.get_fleet_state(operator=COMMANDER))["playback_stopped"] is True
    assert (await gcs_main.get_status())["playback_stopped"] is True


@pytest.mark.asyncio
async def test_a_land_one_vehicle_accepts_is_not_playback_stopped(playing_fleet):
    _refuse(playing_fleet, 0)
    _refuse(playing_fleet, 1)  # drone 2 accepts: the fleet moves to LANDING

    with pytest.raises(HTTPException) as exc:
        await gcs_main.trigger_land(operator=COMMANDER)

    assert exc.value.detail["fleet_state"] == "LANDING"
    assert exc.value.detail["playback_stopped"] is False
    assert (await gcs_main.get_status())["playback_stopped"] is False


@pytest.mark.asyncio
async def test_a_playing_show_is_not_playback_stopped(playing_fleet):
    assert (await gcs_main.get_fleet_state(operator=COMMANDER))["playback_stopped"] is False
    assert (await gcs_main.get_status())["playback_stopped"] is False


@pytest.mark.asyncio
async def test_a_successful_land_clears_it(playing_fleet):
    result = await gcs_main.trigger_land(operator=COMMANDER)
    assert result["status"] == "success"
    assert (await gcs_main.get_status())["playback_stopped"] is False  # LANDING, not RUNNING


def test_the_flag_reaches_the_hud_in_the_slow_frame_without_waiting_for_the_1hz_frame(playing_fleet):
    client = TestClient(gcs_main.app)

    with client.websocket_connect("/ws/telemetry") as ws:
        def next_slow_frame() -> dict:
            while True:
                message = ws.receive()
                if message.get("text"):
                    return json.loads(message["text"])

        assert next_slow_frame()["fleet_state"]["playback_stopped"] is False

        sent_at = time.monotonic()
        playing_fleet.is_running_trajectory = False  # the playback loop stopped; the fleet state did not move
        frame = next_slow_frame()

        assert frame["fleet_state"]["state"] == "RUNNING"
        assert frame["fleet_state"]["playback_stopped"] is True
        # The next scheduled slow frame is ~1s away; this one was pushed on the change.
        assert time.monotonic() - sent_at < 0.8


