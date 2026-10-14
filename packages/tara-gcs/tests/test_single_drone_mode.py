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
async def test_mode_switch_and_status_reflection(commander, viewer):
    res = await gcs_main.set_mode(
        gcs_main.ModeSwitchRequest(mode="HARDWARE", confirmation="CONFIRM_HARDWARE"),
        operator=commander,
    )
    assert res["status"] == "success"
    assert res["mode"] == "HARDWARE"

    status = await gcs_main.get_status()
    assert status["mode"] == "HARDWARE"

    mode_info = await gcs_main.get_mode(operator=viewer)
    assert mode_info["mode"] == "HARDWARE"
    assert mode_info["hardware_flight_available"] is False

    res_back = await gcs_main.set_mode(
        gcs_main.ModeSwitchRequest(mode="SIMULATION", confirmation="CONFIRM_SIMULATION"),
        operator=commander,
    )
    assert res_back["status"] == "success"
    assert res_back["mode"] == "SIMULATION"

    status_back = await gcs_main.get_status()
    assert status_back["mode"] == "SIMULATION"


@pytest.mark.asyncio
async def test_hardware_flight_endpoints_blocked_without_acceptance_gate(commander):
    await gcs_main.set_mode(
        gcs_main.ModeSwitchRequest(mode="HARDWARE", confirmation="CONFIRM_HARDWARE"),
        operator=commander,
    )
    try:
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
    finally:
        await gcs_main.set_mode(
            gcs_main.ModeSwitchRequest(mode="SIMULATION", confirmation="CONFIRM_SIMULATION"),
            operator=commander,
        )


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

    # Descent rate bounds: 0.1 to 1.0 m/s
    with pytest.raises(ValidationError):
        gcs_main.SingleDroneSequenceRequest(descent_rate_mps=0.05)
    with pytest.raises(ValidationError):
        gcs_main.SingleDroneSequenceRequest(descent_rate_mps=2.5)

    # Valid parameters pass cleanly
    req = gcs_main.SingleDroneSequenceRequest(target_altitude_m=1.5, hover_duration_s=5.0, descent_rate_mps=0.5)
    assert req.target_altitude_m == 1.5
    assert req.hover_duration_s == 5.0
    assert req.descent_rate_mps == 0.5


@pytest.mark.asyncio
async def test_single_drone_hover_progression_in_simulation(commander, patched_manager):
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.3)

        result = await gcs_main.run_single_drone_sequence(
            gcs_main.SingleDroneSequenceRequest(target_altitude_m=1.0, hover_duration_s=1.0, descent_rate_mps=0.5),
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
        assert landing_step["descent_rate_mps"] == 0.5
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
