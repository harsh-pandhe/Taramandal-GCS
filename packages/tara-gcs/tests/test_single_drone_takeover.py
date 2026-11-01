"""A pilot taking over mid-hover must stop the single-drone sequence (issue #171).

Observed on real PX4 SITL: a MANUAL_CONTROL stream plus a switch to POSCTL
(MAVSDK ``manual_control.start_position_control()``) moved PX4 out of OFFBOARD
mid-dwell. The sequence never looked at the flight mode, kept "dwelling" for
the rest of the hold, then commanded LAND over the pilot and reported
COMPLETED. These tests drive the same condition on the sim backend by forcing
the flight mode the telemetry reports.
"""

import asyncio
import time

import pytest
from fastapi import HTTPException

from tara_gcs import main as gcs_main
from tara_gcs.auth import Operator, Role
from tara_gcs.drone_manager import DroneManager

TARGET_ALT = 0.3


@pytest.fixture
def commander():
    return Operator(id="test-commander", role=Role.FLIGHT_COMMANDER)


@pytest.fixture
def manager(monkeypatch):
    manager = DroneManager(ports=[14540], backend="sim")
    monkeypatch.setattr(gcs_main, "drone_manager", manager)
    return manager


def _track_commands(manager, monkeypatch):
    """Record every land / recovery the sequence issues itself."""
    calls = {"land": [], "recover": []}
    original_land_one = manager._land_one
    original_recover = manager._safe_recover

    async def tracking_land_one(drone_id):
        calls["land"].append(drone_id)
        return await original_land_one(drone_id)

    async def tracking_recover(drone_id, drone_, tel_, context):
        calls["recover"].append(context)
        await original_recover(drone_id, drone_, tel_, context)

    manager._land_one = tracking_land_one
    monkeypatch.setattr(manager, "_safe_recover", tracking_recover)
    return calls


def _force_flight_mode(manager, monkeypatch, *, takeover_when):
    """Report OFFBOARD once the climb starts, then POSCTL once takeover_when(tel).

    Overrides the sim's own flight_mode field after every telemetry ingest so
    the test controls exactly what the sequence sees.
    """
    original_ingest = manager._ingest_bulk_telemetry
    state = {"taken_over": False}

    def ingest(snap, include_slow):
        original_ingest(snap, include_slow)
        tel = manager.telemetry[0]
        if not tel.get("armed"):
            return
        if not state["taken_over"] and takeover_when(tel):
            state["taken_over"] = True
        tel["flight_mode"] = "POSCTL" if state["taken_over"] else "OFFBOARD"

    monkeypatch.setattr(manager, "_ingest_bulk_telemetry", ingest)
    return state


def _run(commander, hover_s):
    return gcs_main.run_single_drone_sequence(
        gcs_main.SingleDroneSequenceRequest(target_altitude_m=TARGET_ALT, hover_duration_s=hover_s),
        operator=commander,
    )


@pytest.mark.asyncio
async def test_takeover_during_the_dwell_stops_the_sequence_without_commanding_the_drone(
    commander, manager, monkeypatch
):
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)
        calls = _track_commands(manager, monkeypatch)
        reached = {"at": None}

        def after_hover_reached(tel):
            if abs(tel.get("local_z", 0.0) - TARGET_ALT) <= 0.1:
                reached["at"] = reached["at"] or time.monotonic()
            return reached["at"] is not None and time.monotonic() - reached["at"] > 0.5

        _force_flight_mode(manager, monkeypatch, takeover_when=after_hover_reached)

        with pytest.raises(HTTPException) as exc:
            await _run(commander, hover_s=5.0)

        assert exc.value.status_code == 500
        assert "operator took over" in exc.value.detail
        assert "POSCTL" in exc.value.detail
        assert calls["land"] == []  # never landed over the pilot
        assert calls["recover"] == []  # never fought for the vehicle
        assert manager.telemetry[0]["armed"] is True  # and never disarmed it
        assert 0 not in manager.single_drone_sequence_active  # guard released
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_takeover_during_the_climb_stops_the_sequence_without_commanding_the_drone(
    commander, manager, monkeypatch
):
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)
        calls = _track_commands(manager, monkeypatch)
        # Taken over as soon as the vehicle has left the ground.
        _force_flight_mode(
            manager, monkeypatch, takeover_when=lambda tel: tel.get("local_z", 0.0) > 0.05
        )

        with pytest.raises(HTTPException) as exc:
            await _run(commander, hover_s=2.0)

        assert exc.value.status_code == 500
        assert "operator took over" in exc.value.detail
        assert calls["land"] == []
        assert calls["recover"] == []
        assert 0 not in manager.single_drone_sequence_active
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_a_mode_other_than_offboard_before_offboard_engages_is_not_a_takeover(
    commander, manager, monkeypatch
):
    """PX4 reports HOLD/TAKEOFF for a moment after arm, before OFFBOARD: the
    sequence only treats leaving a mode it has seen as a takeover."""
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)
        original_ingest = manager._ingest_bulk_telemetry
        t0 = {}

        def ingest(snap, include_slow):
            original_ingest(snap, include_slow)
            tel = manager.telemetry[0]
            if not tel.get("armed"):
                return
            t0.setdefault("armed_at", time.monotonic())
            lagging = time.monotonic() - t0["armed_at"] < 0.5
            tel["flight_mode"] = "HOLD" if lagging else "OFFBOARD"

        monkeypatch.setattr(manager, "_ingest_bulk_telemetry", ingest)

        result = await _run(commander, hover_s=1.0)

        assert result["status"] == "COMPLETED"
    finally:
        await manager.shutdown()
