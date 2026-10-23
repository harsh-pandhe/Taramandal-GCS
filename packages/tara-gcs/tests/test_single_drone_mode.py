"""Tests for GCS Operating Mode and single-drone hover/landing progression (issue #131).

Verifies:
- Visible operating mode default (SIMULATION).
- Role-gated mode switching (FLIGHT_COMMANDER required).
- Explicit confirmation required to change mode (no accidental / silent switching).
- Telemetry freshness and stale heartbeat reporting.
- Hard safety interlock: hardware hover blocked when uncertified (#135).
- Audit of flight command endpoints failing closed in HARDWARE mode.
- Input bounds checking on target altitude, hover duration, and descent rate.
- Deterministic single-drone 1.0 m hover and slow landing sequence in simulation.
- Verified disarm confirmation and unasserted RC takeover capability.
"""

import asyncio
import time
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from tara_gcs import main as gcs_main
from tara_gcs.auth import Operator, Role
from tara_gcs.drone_manager import DroneManager


@pytest.fixture
def viewer():
    return Operator(id="test-viewer", role=Role.VIEWER)


@pytest.fixture
def commander():
    return Operator(id="test-commander", role=Role.FLIGHT_COMMANDER)


@pytest.fixture
def patched_manager(monkeypatch):
    def _install(manager: DroneManager) -> DroneManager:
        monkeypatch.setattr(gcs_main, "drone_manager", manager)
        return manager
    return _install


@pytest.mark.asyncio
async def test_mode_defaults_to_simulation(viewer):
    mode_info = await gcs_main.get_mode(operator=viewer)
    assert mode_info["mode"] == "SIMULATION"
    assert "hardware_profile" in mode_info
    assert mode_info["hardware_profile"]["autopilot"] == "Pixhawk FMUv2 (ArduPilot Quad-X)"
    assert mode_info["hardware_flight_certified"] is False
    assert mode_info["hardware_flight_available"] is False
    assert mode_info["rc_takeover_available"] is False
    assert mode_info["rc_status"] == "UNVERIFIED"


@pytest.mark.asyncio
async def test_mode_switch_requires_flight_commander(viewer):
    with pytest.raises(Exception):
        await gcs_main.set_mode(
            gcs_main.ModeSwitchRequest(mode="HARDWARE", confirmation="CONFIRM_HARDWARE"),
            operator=viewer,
        )


@pytest.mark.asyncio
async def test_mode_switch_requires_exact_confirmation(commander):
    with pytest.raises(HTTPException) as exc:
        await gcs_main.set_mode(
            gcs_main.ModeSwitchRequest(mode="HARDWARE", confirmation="WRONG_CONFIRMATION"),
            operator=commander,
        )
    assert exc.value.status_code == 400
    assert "Confirmation mismatch" in exc.value.detail


@pytest.mark.asyncio
async def test_mode_switch_to_hardware_rejected_when_uncertified(commander):
    with pytest.raises(HTTPException) as exc:
        await gcs_main.set_mode(
            gcs_main.ModeSwitchRequest(mode="HARDWARE", confirmation="CONFIRM_HARDWARE"),
            operator=commander,
        )
    assert exc.value.status_code == 412
    assert "Cannot switch to HARDWARE mode" in exc.value.detail


@pytest.mark.asyncio
async def test_mode_switch_when_certified_and_status_reflection(commander, viewer, monkeypatch):
    monkeypatch.setattr(gcs_main, "hardware_flight_certified", True)
    monkeypatch.setattr(gcs_main.drone_manager, "backend", "hardware")

    res = await gcs_main.set_mode(
        gcs_main.ModeSwitchRequest(mode="HARDWARE", confirmation="CONFIRM_HARDWARE"),
        operator=commander,
    )
    assert res["status"] == "success"
    assert res["mode"] == "HARDWARE"
    assert res["active_backend"] == "hardware"

    status = await gcs_main.get_status()
    assert status["mode"] == "HARDWARE"
    assert status["active_backend"] == "hardware"
    assert status["hardware_flight_certified"] is True
    assert status["hardware_flight_available"] is True

    mode_info = await gcs_main.get_mode(operator=viewer)
    assert mode_info["mode"] == "HARDWARE"
    assert mode_info["active_backend"] == "hardware"
    assert mode_info["hardware_flight_certified"] is True
    assert mode_info["hardware_flight_available"] is True

    res_back = await gcs_main.set_mode(
        gcs_main.ModeSwitchRequest(mode="SIMULATION", confirmation="CONFIRM_SIMULATION"),
        operator=commander,
    )
    assert res_back["status"] == "success"
    assert res_back["mode"] == "SIMULATION"

    status_back = await gcs_main.get_status()
    assert status_back["mode"] == "SIMULATION"


@pytest.mark.asyncio
async def test_startup_refuses_hardware_mode_when_uncertified(monkeypatch):
    monkeypatch.setattr(gcs_main, "current_operating_mode", "HARDWARE")
    monkeypatch.setattr(gcs_main, "hardware_flight_certified", False)
    with pytest.raises(RuntimeError) as exc:
        await gcs_main.startup_event()
    assert "GCS startup refused" in str(exc.value)


@pytest.mark.asyncio
async def test_hardware_flight_endpoints_blocked_without_acceptance_gate(commander, monkeypatch):
    monkeypatch.setattr(gcs_main, "current_operating_mode", "HARDWARE")
    monkeypatch.setattr(gcs_main, "hardware_flight_certified", False)

    # Hover sequence blocked with 412
    with pytest.raises(HTTPException) as exc1:
        await gcs_main.run_single_drone_sequence(
            gcs_main.SingleDroneSequenceRequest(target_altitude_m=1.0, hover_duration_s=2.0),
            operator=commander,
        )
    assert exc1.value.status_code == 412
    assert "Hardware acceptance gate #135 has not passed" in exc1.value.detail

    # Fleet launch blocked with 412
    with pytest.raises(HTTPException) as exc2:
        await gcs_main.launch_fleet(operator=commander)
    assert exc2.value.status_code == 412
    assert "Hardware acceptance gate #135 has not passed" in exc2.value.detail

    # Trajectory start blocked with 412
    with pytest.raises(HTTPException) as exc3:
        await gcs_main.start_trajectory(
            gcs_main.StartTrajectoryRequest(mode="LOCAL", execution="SIMULATED"),
            operator=commander,
        )
    assert exc3.value.status_code == 412
    assert "Hardware acceptance gate #135 has not passed" in exc3.value.detail

    # Resume show blocked with 412
    with pytest.raises(HTTPException) as exc4:
        await gcs_main.resume_show(operator=commander)
    assert exc4.value.status_code == 412
    assert "Hardware acceptance gate #135 has not passed" in exc4.value.detail


@pytest.mark.asyncio
async def test_emergency_recovery_endpoints_truthful_and_fail_closed(commander, monkeypatch):
    # In SIMULATION mode with sim backend, emergency actions report active backend truthfully
    rtl_res = await gcs_main.trigger_rtl(operator=commander)
    assert rtl_res["status"] == "success"
    assert rtl_res["backend"] == gcs_main.drone_manager.backend

    land_res = await gcs_main.trigger_land(operator=commander)
    assert land_res["status"] == "success"
    assert land_res["backend"] == gcs_main.drone_manager.backend

    estop_res = await gcs_main.trigger_estop(operator=commander)
    assert estop_res["status"] == "success"
    assert estop_res["backend"] == gcs_main.drone_manager.backend

    # In HARDWARE mode without a hardware backend, emergency routes fail closed with 503
    monkeypatch.setattr(gcs_main, "current_operating_mode", "HARDWARE")
    monkeypatch.setattr(gcs_main.drone_manager, "backend", "sim")

    with pytest.raises(HTTPException) as exc1:
        await gcs_main.trigger_rtl(operator=commander)
    assert exc1.value.status_code == 503
    assert "Physical emergency RTL unavailable" in exc1.value.detail

    with pytest.raises(HTTPException) as exc2:
        await gcs_main.trigger_land(operator=commander)
    assert exc2.value.status_code == 503
    assert "Physical emergency landing unavailable" in exc2.value.detail

    with pytest.raises(HTTPException) as exc3:
        await gcs_main.trigger_estop(operator=commander)
    assert exc3.value.status_code == 503
    assert "Physical emergency stop unavailable" in exc3.value.detail

    with pytest.raises(HTTPException) as exc4:
        await gcs_main.abort_drone_rtl(drone_id=0, operator=commander)
    assert exc4.value.status_code == 503
    assert "Physical emergency RTL unavailable" in exc4.value.detail


def test_single_drone_sequence_request_bounds():
    # Target altitude bounds: 0.3 to 5.0m
    with pytest.raises(ValidationError):
        gcs_main.SingleDroneSequenceRequest(target_altitude_m=0.1)
    with pytest.raises(ValidationError):
        gcs_main.SingleDroneSequenceRequest(target_altitude_m=10.0)

    # Hover duration bounds: 1.0 to 30.0s
    with pytest.raises(ValidationError):
        gcs_main.SingleDroneSequenceRequest(hover_duration_s=0.5)
    with pytest.raises(ValidationError):
        gcs_main.SingleDroneSequenceRequest(hover_duration_s=45.0)

    # Valid parameters pass cleanly
    req = gcs_main.SingleDroneSequenceRequest(target_altitude_m=1.5, hover_duration_s=5.0)
    assert req.target_altitude_m == 1.5
    assert req.hover_duration_s == 5.0


@pytest.mark.asyncio
async def test_single_drone_hover_progression_in_simulation(commander, patched_manager):
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.3)

        result = await gcs_main.run_single_drone_sequence(
            gcs_main.SingleDroneSequenceRequest(target_altitude_m=1.0, hover_duration_s=1.0),
            operator=commander,
        )

        assert result["status"] == "COMPLETED"
        assert result["mode"] == "SIMULATION"
        assert result["vehicle"] == "SIMULATED"

        steps = [s["step"] for s in result["steps"]]
        assert "PREFLIGHT" in steps
        assert "ARM" in steps
        assert "HOVER_REACHED" in steps
        assert "HOVER_HOLD_COMPLETE" in steps
        assert "LANDING_COMMANDED" in steps
        assert "TOUCHDOWN_DISARM" in steps

        hover_step = next(s for s in result["steps"] if s["step"] == "HOVER_HOLD_COMPLETE")
        assert hover_step["dwell_s"] >= 0.95
        assert hover_step["status"] == "STABLE"

        landing_step = next(s for s in result["steps"] if s["step"] == "LANDING_COMMANDED")
        assert landing_step["landing_control"] == "AUTOPILOT_DELEGATED"
        assert "Descent rate is governed by autopilot" in landing_step["note"]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_single_drone_fails_closed_on_disarm_timeout(commander, patched_manager, monkeypatch):
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.3)

        # Intercept armed check during landing so disarm is never confirmed by telemetry
        original_sleep = asyncio.sleep
        async def keep_armed_sleep(duration):
            manager.telemetry[0]["armed"] = True
            await original_sleep(duration)
        monkeypatch.setattr(asyncio, "sleep", keep_armed_sleep)

        with pytest.raises(HTTPException) as exc:
            await gcs_main.run_single_drone_sequence(
                gcs_main.SingleDroneSequenceRequest(target_altitude_m=0.5, hover_duration_s=1.0),
                operator=commander,
            )
        assert exc.value.status_code == 500
        assert "Touchdown disarm could not be confirmed" in exc.value.detail
        # Assert tel['armed'] was NOT mutated or fabricated to False
        assert manager.telemetry[0]["armed"] is True
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_single_drone_fails_closed_on_mid_air_disarm(commander, patched_manager, monkeypatch):
    """issue #145: a drone that disarms mid-air (fault, failsafe, a bug) must
    never be reported as a normal landing. Before this fix, landed_state was
    never populated, so the touchdown check read it as vacuously "landed" and
    this exact scenario would have reported DISARMED/success."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.3)

        # Let arming happen for real (it's confirmed within ~0.05-0.1s - see
        # test_single_drone_sim_reports_on_ground_after_a_real_landing, which
        # doesn't patch anything), then force every subsequent telemetry sync
        # to report drone 0 as disarmed but still airborne - faking a
        # fault/failsafe mid-air disarm. Overriding the sync itself (rather
        # than the value between syncs) means the sim's own background loop
        # can't race this by converging to a real landed state in between.
        # Gated on elapsed wall-clock time, not an "armed observed" flag:
        # that flag would race the production arm-confirmation poll for the
        # same tick's value and could flip it back before that poll ever
        # observes True.
        original_ingest = manager._ingest_bulk_telemetry
        start = time.monotonic()
        def faking_ingest(snap, include_slow):
            original_ingest(snap, include_slow)
            if time.monotonic() - start > 1.0:
                tel = manager.telemetry[0]
                tel["armed"] = False
                tel["landed_state"] = "IN_AIR"
        monkeypatch.setattr(manager, "_ingest_bulk_telemetry", faking_ingest)

        with pytest.raises(HTTPException) as exc:
            await gcs_main.run_single_drone_sequence(
                gcs_main.SingleDroneSequenceRequest(target_altitude_m=0.5, hover_duration_s=1.0),
                operator=commander,
            )
        assert exc.value.status_code == 500
        assert "Touchdown disarm could not be confirmed" in exc.value.detail
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_single_drone_sim_reports_on_ground_after_a_real_landing(commander, patched_manager):
    """issue #145: landed_state is populated from real sim physics (altitude
    and vertical speed), not merely inferred from disarm - a genuine landing
    reports ON_GROUND independently of the disarm check."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.3)
        assert manager.telemetry[0]["landed_state"] == "ON_GROUND"

        result = await gcs_main.run_single_drone_sequence(
            gcs_main.SingleDroneSequenceRequest(target_altitude_m=0.5, hover_duration_s=1.0),
            operator=commander,
        )
        assert result["steps"][-1]["step"] == "TOUCHDOWN_DISARM"
        assert result["steps"][-1]["status"] == "DISARMED"
        assert manager.telemetry[0]["landed_state"] == "ON_GROUND"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_single_drone_fails_closed_when_disconnected(commander, patched_manager):
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        manager.telemetry[0]["connected"] = False

        with pytest.raises(HTTPException) as exc:
            await gcs_main.run_single_drone_sequence(
                gcs_main.SingleDroneSequenceRequest(target_altitude_m=1.0, hover_duration_s=1.0),
                operator=commander,
            )
        assert exc.value.status_code == 400
        assert "Drone 0 is not connected" in exc.value.detail
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_single_drone_fails_closed_on_stale_telemetry(commander, patched_manager):
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.3)
        # Advance heartbeat to simulate stale link
        manager.telemetry[0]["last_heartbeat"] = time.time() - 10.0

        with pytest.raises(HTTPException) as exc:
            await gcs_main.run_single_drone_sequence(
                gcs_main.SingleDroneSequenceRequest(target_altitude_m=1.0, hover_duration_s=1.0),
                operator=commander,
            )
        assert exc.value.status_code == 500
        assert "stale or missing heartbeat" in exc.value.detail
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_telemetry_freshness_tracking(viewer, patched_manager):
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.3)

        mode_info = await gcs_main.get_mode(operator=viewer)
        assert mode_info["telemetry_status"] == "FRESH"
        assert mode_info["telemetry_freshness_s"] is not None
        assert mode_info["telemetry_freshness_s"] < 2.5

        # Simulate link loss by advancing time on heartbeat
        manager.telemetry[0]["last_heartbeat"] = time.time() - 5.0
        mode_stale = await gcs_main.get_mode(operator=viewer)
        assert mode_stale["telemetry_status"] == "STALE"
        assert mode_stale["telemetry_freshness_s"] >= 5.0
    finally:
        await manager.shutdown()

