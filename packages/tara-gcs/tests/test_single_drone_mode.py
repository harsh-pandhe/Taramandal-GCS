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
from tara_gcs.fleet_fsm import LifecycleState


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
        # 0.3s is tara_sim's own DEFAULT_ARMABLE_DELAY_S; 0.5s clears it with
        # margin so the new pre-arm gate check (issue #145 review, item 2)
        # doesn't race the sim's own settle timer.
        await asyncio.sleep(0.5)

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
        # 0.3s is tara_sim's own DEFAULT_ARMABLE_DELAY_S; 0.5s clears it with
        # margin so the new pre-arm gate check (issue #145 review, item 2)
        # doesn't race the sim's own settle timer.
        await asyncio.sleep(0.5)

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
        # 0.3s is tara_sim's own DEFAULT_ARMABLE_DELAY_S; 0.5s clears it with
        # margin so the new pre-arm gate check (issue #145 review, item 2)
        # doesn't race the sim's own settle timer.
        await asyncio.sleep(0.5)

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
async def test_single_drone_fails_closed_when_landed_state_is_unknown(commander, patched_manager, monkeypatch):
    """issue #145 review (Vishal): landed_state == None must not count as
    landed. The old "is None or == ON_GROUND" fallback meant a drone whose
    landed-state stream simply hadn't reported yet (not necessarily disarmed
    mid-air - just unconfirmed) could still pass the touchdown check.

    Fails closed faster now than it used to (a stale-telemetry error instead
    of waiting out the full disarm-confirmation deadline): a review follow-up
    made landed_state_at == None itself count as stale, same as a heartbeat
    that was never set, rather than only timing out once it goes stale with
    age. Either way this must still fail closed, never report DISARMED.
    """
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        # 0.3s is tara_sim's own DEFAULT_ARMABLE_DELAY_S; 0.5s clears it with
        # margin so the new pre-arm gate check (issue #145 review, item 2)
        # doesn't race the sim's own settle timer.
        await asyncio.sleep(0.5)

        original_ingest = manager._ingest_bulk_telemetry
        start = time.monotonic()
        def faking_ingest(snap, include_slow):
            original_ingest(snap, include_slow)
            if time.monotonic() - start > 1.0:
                tel = manager.telemetry[0]
                tel["armed"] = False
                tel["landed_state"] = None
                tel["landed_state_at"] = None
        monkeypatch.setattr(manager, "_ingest_bulk_telemetry", faking_ingest)

        with pytest.raises(HTTPException) as exc:
            await gcs_main.run_single_drone_sequence(
                gcs_main.SingleDroneSequenceRequest(target_altitude_m=0.5, hover_duration_s=1.0),
                operator=commander,
            )
        assert exc.value.status_code == 500
        assert "landed-state telemetry became stale" in exc.value.detail
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_single_drone_fails_closed_on_stale_landed_state(commander, patched_manager, monkeypatch):
    """issue #145 review (Vishal): landed_state needs its own freshness check,
    the same way last_heartbeat already has one - a stream that stops
    updating while reporting a stale ON_GROUND must not be trusted."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        # 0.3s is tara_sim's own DEFAULT_ARMABLE_DELAY_S; 0.5s clears it with
        # margin so the new pre-arm gate check (issue #145 review, item 2)
        # doesn't race the sim's own settle timer.
        await asyncio.sleep(0.5)

        original_ingest = manager._ingest_bulk_telemetry
        start = time.monotonic()
        def faking_ingest(snap, include_slow):
            original_ingest(snap, include_slow)
            if time.monotonic() - start > 1.0:
                tel = manager.telemetry[0]
                tel["armed"] = False
                tel["landed_state"] = "ON_GROUND"
                tel["landed_state_at"] = time.time() - 10.0  # well past the 2.5s staleness window
        monkeypatch.setattr(manager, "_ingest_bulk_telemetry", faking_ingest)

        with pytest.raises(HTTPException) as exc:
            await gcs_main.run_single_drone_sequence(
                gcs_main.SingleDroneSequenceRequest(target_altitude_m=0.5, hover_duration_s=1.0),
                operator=commander,
            )
        assert exc.value.status_code == 500
        assert "landed-state telemetry became stale" in exc.value.detail
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_single_drone_hover_refused_when_fleet_not_parked(commander, patched_manager):
    """issue #145 review (Vishal), item 2: the endpoint used to have no
    fleet-FSM legality check at all - it could be invoked while the fleet
    FSM was mid-show (RUNNING etc.), not just when the fleet happened to be
    STAGED/DISARMED."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)
        manager.fleet_fsm._state = LifecycleState.RUNNING

        with pytest.raises(HTTPException) as exc:
            await gcs_main.run_single_drone_sequence(
                gcs_main.SingleDroneSequenceRequest(target_altitude_m=0.5, hover_duration_s=1.0),
                operator=commander,
            )
        assert exc.value.status_code == 409
        assert exc.value.detail["command"] == "single_drone_hover"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_single_drone_hover_refused_while_fleet_show_flag_is_set(commander, patched_manager):
    """issue #145 review (Vishal), item 2: defense-in-depth alongside the FSM
    check, the same way /api/launch double-checks is_running_trajectory /
    active_trajectory rather than trusting the FSM alone - the FSM can be
    stale (e.g. right after a restart) while a show is genuinely active."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)
        manager.is_running_trajectory = True

        with pytest.raises(HTTPException) as exc:
            await gcs_main.run_single_drone_sequence(
                gcs_main.SingleDroneSequenceRequest(target_altitude_m=0.5, hover_duration_s=1.0),
                operator=commander,
            )
        assert exc.value.status_code == 409
        assert "fleet show is active" in exc.value.detail
    finally:
        manager.is_running_trajectory = False
        await manager.shutdown()


@pytest.mark.asyncio
async def test_single_drone_hover_refused_when_already_running(commander, patched_manager):
    """issue #145 review (Vishal), item 2: concurrency guard - two overlapping
    requests on the same drone must not both try to drive it."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)
        manager.single_drone_sequence_active.add(0)

        with pytest.raises(HTTPException) as exc:
            await gcs_main.run_single_drone_sequence(
                gcs_main.SingleDroneSequenceRequest(target_altitude_m=0.5, hover_duration_s=1.0),
                operator=commander,
            )
        assert exc.value.status_code == 409
        assert "already running" in exc.value.detail
    finally:
        manager.single_drone_sequence_active.discard(0)
        await manager.shutdown()


@pytest.mark.asyncio
async def test_single_drone_hover_refused_when_preflight_gate_red(commander, patched_manager, monkeypatch):
    """issue #145 review (Vishal), item 2: no pre-arm gate (GPS/EKF/battery)
    was ever checked before this endpoint armed and flew the drone. A red
    gate (here: EKF/home lock never settling) must refuse before anything
    moves, with the failing gate named in the response."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)

        original_ingest = manager._ingest_bulk_telemetry
        def never_armable(snap, include_slow):
            original_ingest(snap, include_slow)
            manager.telemetry[0]["health_armable"] = False
            manager.telemetry[0]["health_home_ok"] = False
        monkeypatch.setattr(manager, "_ingest_bulk_telemetry", never_armable)
        await asyncio.sleep(0.2)

        with pytest.raises(HTTPException) as exc:
            await gcs_main.run_single_drone_sequence(
                gcs_main.SingleDroneSequenceRequest(target_altitude_m=0.5, hover_duration_s=1.0),
                operator=commander,
            )
        assert exc.value.status_code == 412
        assert "pre-arm gates are red" in exc.value.detail["message"]
        assert any("home lock" in reason for reason in exc.value.detail["blocking_reasons"])
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_single_drone_hover_records_audit_entries(commander, patched_manager):
    """issue #145 review (Vishal), item 2: no command_audit.record at all
    before - a real flight command with nothing in the operator-facing
    audit trail (/api/audit/commands)."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)
        before = len(gcs_main.command_audit.recent)

        await gcs_main.run_single_drone_sequence(
            gcs_main.SingleDroneSequenceRequest(target_altitude_m=0.5, hover_duration_s=1.0),
            operator=commander,
        )

        new_entries = list(gcs_main.command_audit.recent)[before:]
        events = [e["event"] for e in new_entries]
        assert "single_drone_hover_requested" in events
        assert "single_drone_hover_completed" in events
        requested = next(e for e in new_entries if e["event"] == "single_drone_hover_requested")
        assert requested["drone_id"] == 0
        assert requested["operator"] == commander.id
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_single_drone_climb_reports_the_altitude_actually_reached(commander, patched_manager):
    """issue #145 review (Vishal), item 3: HOVER_REACHED used to fire from a
    fixed sleep, which says nothing about whether the climb actually reached
    (or stalled short of) the target. It must now report the altitude
    telemetry actually observed, within tolerance of the request."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        # 0.3s is tara_sim's own DEFAULT_ARMABLE_DELAY_S; 0.5s clears it with
        # margin so the new pre-arm gate check (issue #145 review, item 2)
        # doesn't race the sim's own settle timer.
        await asyncio.sleep(0.5)

        result = await gcs_main.run_single_drone_sequence(
            gcs_main.SingleDroneSequenceRequest(target_altitude_m=0.8, hover_duration_s=1.0),
            operator=commander,
        )
        hover_reached = next(s for s in result["steps"] if s["step"] == "HOVER_REACHED")
        assert abs(hover_reached["altitude_m"] - 0.8) <= 0.15
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_single_drone_climb_stall_fails_closed_and_recovers(commander, patched_manager, monkeypatch):
    """issue #145 review (Vishal), item 3 and item 5 together: a climb that
    never reaches the target altitude (offboard silently rejected, stuck
    vehicle) must be detected and fail closed, rather than a fixed sleep
    reporting HOVER_REACHED regardless. Since arm() already succeeded by
    this point, the failure must also attempt a safe recovery rather than
    leaving the vehicle armed.

    issue #145 review (Harsh, real PX4, 2026-10-27): the climb no longer
    fails on a fixed deadline but on stalled progress (see
    DroneManager.SINGLE_DRONE_CLIMB_STALL_WINDOW_S); shrink that window here
    so this test doesn't have to wait out the real 10s to prove it."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    monkeypatch.setattr(manager, "SINGLE_DRONE_CLIMB_STALL_WINDOW_S", 0.5)
    try:
        await manager.connect_all()
        # 0.3s is tara_sim's own DEFAULT_ARMABLE_DELAY_S; 0.5s clears it with
        # margin so the new pre-arm gate check (issue #145 review, item 2)
        # doesn't race the sim's own settle timer.
        await asyncio.sleep(0.5)

        # Pin the reported altitude at ground level regardless of what the
        # real sim physics underneath does, faking a climb that never
        # progresses.
        original_ingest = manager._ingest_bulk_telemetry
        def freeze_altitude(snap, include_slow):
            original_ingest(snap, include_slow)
            manager.telemetry[0]["local_z"] = 0.0
        monkeypatch.setattr(manager, "_ingest_bulk_telemetry", freeze_altitude)

        recover_calls = []
        original_recover = manager._safe_recover
        async def tracking_recover(drone_id, drone, tel, context):
            recover_calls.append(context)
            await original_recover(drone_id, drone, tel, context)
        monkeypatch.setattr(manager, "_safe_recover", tracking_recover)

        with pytest.raises(HTTPException) as exc:
            await gcs_main.run_single_drone_sequence(
                gcs_main.SingleDroneSequenceRequest(target_altitude_m=0.5, hover_duration_s=1.0),
                operator=commander,
            )
        assert exc.value.status_code == 500
        assert "climb stalled" in exc.value.detail
        assert recover_calls == ["after a failed climb"]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_single_drone_climb_tolerates_delayed_liftoff_and_slow_climb(commander, patched_manager, monkeypatch):
    """issue #145 review (Harsh, real PX4 SITL + Gazebo, 2026-10-27): real
    PX4 spends several seconds arming/spooling up before it moves at all,
    then climbs at ~0.2 m/s - both slower than the old fixed-deadline
    formula (target_alt_m / 0.5 + 5.0s) assumed, which is why a genuinely
    still-climbing vehicle twice got reported as having failed to reach the
    target. The new stall-progress check must tolerate a liftoff delay and a
    slow, but still genuinely progressing, climb - and must still reach
    HOVER_REACHED, not time out."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)

        target_alt = 1.0
        liftoff_delay_s = 6.0  # within the observed 4.6-7.5s real-PX4 range
        climb_rate_m_s = 0.2   # the real-PX4 rate observed, not tara_sim's faster one
        start = time.monotonic()
        original_ingest = manager._ingest_bulk_telemetry
        def slow_real_px4_climb(snap, include_slow):
            original_ingest(snap, include_slow)
            elapsed = time.monotonic() - start
            climbing_for = max(0.0, elapsed - liftoff_delay_s)
            simulated_z = min(target_alt, climbing_for * climb_rate_m_s)
            manager.telemetry[0]["local_z"] = simulated_z
        monkeypatch.setattr(manager, "_ingest_bulk_telemetry", slow_real_px4_climb)

        result = await gcs_main.run_single_drone_sequence(
            gcs_main.SingleDroneSequenceRequest(target_altitude_m=target_alt, hover_duration_s=1.0),
            operator=commander,
        )
        hover_reached = next(s for s in result["steps"] if s["step"] == "HOVER_REACHED")
        assert abs(hover_reached["altitude_m"] - target_alt) <= 0.15
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_single_drone_hover_dwell_fails_closed_on_disconnect(commander, patched_manager, monkeypatch):
    """issue #145 review (Vishal), item 4: the hover dwell used to be a blind
    sleep with no disconnect/staleness check - a drone that dropped link
    mid-dwell was indistinguishable from one holding steady. Disconnects it
    only once the climb has clearly finished, so the climb phase (item 3)
    isn't what trips this assertion."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        # 0.3s is tara_sim's own DEFAULT_ARMABLE_DELAY_S; 0.5s clears it with
        # margin so the new pre-arm gate check (issue #145 review, item 2)
        # doesn't race the sim's own settle timer.
        await asyncio.sleep(0.5)

        target_alt = 0.3
        # Match the climb loop's own break-out tolerance (max(0.15, 0.1 *
        # target_alt_m)) exactly, not a looser ad-hoc value: a looser
        # threshold here starts this timer before the climb loop itself
        # considers the target reached, racing the climb's own stall/ceiling
        # checks against this test's disconnect injection (issue #145
        # review, Harsh, 2026-10-27 - flagged as the flakiness root cause).
        alt_tolerance_m = max(0.15, 0.1 * target_alt)
        original_ingest = manager._ingest_bulk_telemetry
        state = {"first_reached_at": None}
        def disconnect_after_climb(snap, include_slow):
            original_ingest(snap, include_slow)
            tel = manager.telemetry[0]
            if abs(tel.get("local_z", 0.0) - target_alt) <= alt_tolerance_m:
                if state["first_reached_at"] is None:
                    state["first_reached_at"] = time.monotonic()
                elif time.monotonic() - state["first_reached_at"] > 0.3:
                    tel["connected"] = False
        monkeypatch.setattr(manager, "_ingest_bulk_telemetry", disconnect_after_climb)

        with pytest.raises(HTTPException) as exc:
            await gcs_main.run_single_drone_sequence(
                gcs_main.SingleDroneSequenceRequest(target_altitude_m=target_alt, hover_duration_s=3.0),
                operator=commander,
            )
        assert exc.value.status_code == 500
        assert "disconnected during hover dwell" in exc.value.detail
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_safe_recover_lands_and_disarms_only_when_armed(commander, patched_manager):
    """issue #145 review (Vishal), item 5, as a focused unit test of the
    recovery helper itself: it must always attempt a land, but only attempt
    a disarm when telemetry actually reports the vehicle armed."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        # 0.3s is tara_sim's own DEFAULT_ARMABLE_DELAY_S; 0.5s clears it with
        # margin so the new pre-arm gate check (issue #145 review, item 2)
        # doesn't race the sim's own settle timer.
        await asyncio.sleep(0.5)
        drone = manager.drones[0]
        tel = manager.telemetry[0]

        land_calls = []
        original_land_one = manager._land_one
        async def tracking_land_one(drone_id):
            land_calls.append(drone_id)
            return await original_land_one(drone_id)
        manager._land_one = tracking_land_one

        disarm_calls = []
        original_disarm = drone.action.disarm
        async def tracking_disarm():
            disarm_calls.append(True)
            return await original_disarm()
        drone.action.disarm = tracking_disarm

        tel["armed"] = False
        await manager._safe_recover(0, drone, tel, context="test, not armed")
        assert land_calls == [0]
        assert disarm_calls == []

        tel["armed"] = True
        await manager._safe_recover(0, drone, tel, context="test, armed")
        assert land_calls == [0, 0]
        assert disarm_calls == [True]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_safe_recover_does_not_disarm_while_still_airborne(commander, patched_manager, monkeypatch):
    """issue #145 review follow-up: _land_one only *sends* the land command,
    it never waits for touchdown - disarming right after it (the first
    version of _safe_recover) could disarm a drone that is still airborne.
    Forces landed_state to stay IN_AIR for the whole 10s deadline and
    asserts no disarm is ever sent."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)
        drone = manager.drones[0]
        tel = manager.telemetry[0]

        original_ingest = manager._ingest_bulk_telemetry
        def still_airborne(snap, include_slow):
            original_ingest(snap, include_slow)
            t = manager.telemetry[0]
            t["landed_state"] = "IN_AIR"
            t["landed_state_at"] = time.time()
            # This drone was never actually flown/armed via the real arm()
            # flow, so the sim's own ground truth keeps reporting disarmed -
            # every real tick would otherwise overwrite the fake "still
            # armed" state this test needs below right back to False.
            t["armed"] = True
        monkeypatch.setattr(manager, "_ingest_bulk_telemetry", still_airborne)
        # Set the initial values too - whatever the real sim last reported
        # (likely a fresh ON_GROUND, parked and never flown) would otherwise
        # satisfy the ground check on the very first poll, before the patch
        # above ever gets a tick in.
        tel["landed_state"] = "IN_AIR"
        tel["landed_state_at"] = time.time()

        disarm_calls = []
        original_disarm = drone.action.disarm
        async def tracking_disarm():
            disarm_calls.append(True)
            return await original_disarm()
        drone.action.disarm = tracking_disarm

        tel["armed"] = True
        await manager._safe_recover(0, drone, tel, context="test, stays airborne")

        assert disarm_calls == []
        assert tel["armed"] is True  # never fabricated to False either
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_safe_recover_disarms_only_after_ground_is_confirmed(commander, patched_manager, monkeypatch):
    """issue #145 review follow-up: once landed_state genuinely reports
    ON_GROUND, _safe_recover must disarm - not stay stuck waiting, and not
    have disarmed before that confirmation existed."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)
        drone = manager.drones[0]
        tel = manager.telemetry[0]

        start = time.monotonic()
        original_ingest = manager._ingest_bulk_telemetry
        def airborne_then_grounded(snap, include_slow):
            original_ingest(snap, include_slow)
            t = manager.telemetry[0]
            t["landed_state"] = "IN_AIR" if time.monotonic() - start < 0.5 else "ON_GROUND"
            t["landed_state_at"] = time.time()
            # Same reasoning as the test above: force "still armed" every
            # tick, since this drone's real sim state was never armed.
            t["armed"] = True
        monkeypatch.setattr(manager, "_ingest_bulk_telemetry", airborne_then_grounded)
        # Same race as the test above: set the initial value explicitly so
        # a pre-existing fresh ON_GROUND can't satisfy the check before the
        # patch's own first tick runs.
        tel["landed_state"] = "IN_AIR"
        tel["landed_state_at"] = time.time()

        disarm_calls = []
        original_disarm = drone.action.disarm
        async def tracking_disarm():
            disarm_calls.append(time.monotonic() - start)
            return await original_disarm()
        drone.action.disarm = tracking_disarm

        tel["armed"] = True
        await manager._safe_recover(0, drone, tel, context="test, grounds after a delay")

        assert len(disarm_calls) == 1
        assert disarm_calls[0] >= 0.5
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_fleet_commands_refused_while_single_drone_sequence_is_active(commander, patched_manager):
    """issue #145 review, item 2: the fleet FSM alone doesn't catch this -
    SINGLE_DRONE_HOVER deliberately never changes the fleet's own lifecycle
    state, so without an explicit guard a launch or upload could start
    mid-hover. Covers launch, upload-mission, resume-show and discard-show
    directly; upload-trajectory shares the exact same guard call, checked
    separately in its own test module."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)
        manager.single_drone_sequence_active.add(0)

        with pytest.raises(HTTPException) as exc1:
            await gcs_main.launch_fleet(operator=commander)
        assert exc1.value.status_code == 409
        assert "single-drone sequence is active" in exc1.value.detail

        with pytest.raises(HTTPException) as exc2:
            await gcs_main.upload_mission(operator=commander)
        assert exc2.value.status_code == 409
        assert "single-drone sequence is active" in exc2.value.detail

        with pytest.raises(HTTPException) as exc3:
            await gcs_main.resume_show(operator=commander)
        assert exc3.value.status_code == 409
        assert "single-drone sequence is active" in exc3.value.detail

        with pytest.raises(HTTPException) as exc4:
            await gcs_main.discard_show(operator=commander)
        assert exc4.value.status_code == 409
        assert "single-drone sequence is active" in exc4.value.detail
    finally:
        manager.single_drone_sequence_active.discard(0)
        await manager.shutdown()


@pytest.mark.asyncio
async def test_single_drone_sim_reports_on_ground_after_a_real_landing(commander, patched_manager):
    """issue #145: landed_state is populated from real sim physics (altitude
    and vertical speed), not merely inferred from disarm - a genuine landing
    reports ON_GROUND independently of the disarm check."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        # 0.3s is tara_sim's own DEFAULT_ARMABLE_DELAY_S; 0.5s clears it with
        # margin so the new pre-arm gate check (issue #145 review, item 2)
        # doesn't race the sim's own settle timer.
        await asyncio.sleep(0.5)
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
        # 0.3s is tara_sim's own DEFAULT_ARMABLE_DELAY_S; 0.5s clears it with
        # margin so the new pre-arm gate check (issue #145 review, item 2)
        # doesn't race the sim's own settle timer.
        await asyncio.sleep(0.5)
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
        # 0.3s is tara_sim's own DEFAULT_ARMABLE_DELAY_S; 0.5s clears it with
        # margin so the new pre-arm gate check (issue #145 review, item 2)
        # doesn't race the sim's own settle timer.
        await asyncio.sleep(0.5)

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


# --- issue #145 re-review (Vishal, head 93d9b63): B1-B4 and the missing tests ---

@pytest.mark.asyncio
async def test_single_drone_fallback_disarm_waits_for_ground_not_a_timeout(commander, patched_manager, monkeypatch):
    """B1: the main landing path's fallback used to call disarm()
    unconditionally once the 10s observation window ran out - the same
    in-flight-disarm risk _safe_recover was already fixed for, left in this
    separate path. Pins landed_state at IN_AIR for the whole window and
    asserts disarm() is never sent."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)
        drone = manager.drones[0]

        original_ingest = manager._ingest_bulk_telemetry
        def never_grounded(snap, include_slow):
            original_ingest(snap, include_slow)
            t = manager.telemetry[0]
            t["landed_state"] = "IN_AIR"
            t["landed_state_at"] = time.time()
        monkeypatch.setattr(manager, "_ingest_bulk_telemetry", never_grounded)

        disarm_calls = []
        original_disarm = drone.action.disarm
        async def tracking_disarm():
            disarm_calls.append(True)
            return await original_disarm()
        drone.action.disarm = tracking_disarm

        with pytest.raises(HTTPException) as exc:
            await gcs_main.run_single_drone_sequence(
                gcs_main.SingleDroneSequenceRequest(target_altitude_m=0.3, hover_duration_s=1.0),
                operator=commander,
            )
        assert exc.value.status_code == 500
        assert "Touchdown disarm could not be confirmed" in exc.value.detail
        assert disarm_calls == []
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_single_drone_land_rejected_attempts_recovery(commander, patched_manager, monkeypatch):
    """B2: offboard.stop() has already run inside _land_one by the time
    action.land() can be rejected (e.g. COMMAND_DENIED) - the vehicle is then
    neither in offboard nor landing, with no recovery attempt at all. Must
    now call _safe_recover before re-raising."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)
        drone = manager.drones[0]
        tel = manager.telemetry[0]

        async def rejected_land():
            raise RuntimeError("COMMAND_DENIED")
        drone.action.land = rejected_land

        recover_calls = []
        original_recover = manager._safe_recover
        async def tracking_recover(drone_id, drone_, tel_, context):
            recover_calls.append(context)
            await original_recover(drone_id, drone_, tel_, context)
        monkeypatch.setattr(manager, "_safe_recover", tracking_recover)

        with pytest.raises(HTTPException) as exc:
            await gcs_main.run_single_drone_sequence(
                gcs_main.SingleDroneSequenceRequest(target_altitude_m=0.3, hover_duration_s=1.0),
                operator=commander,
            )
        assert exc.value.status_code == 500
        assert "Autopilot rejected land command" in exc.value.detail
        assert recover_calls == ["after a failed landing"]
        assert tel["connected"] is True  # sim still alive, just not grounded yet - sanity check
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_single_drone_hover_yields_to_fleet_emergency_state(commander, patched_manager, monkeypatch):
    """B3: the hover previously wasn't emergency-aware at all - an operator
    RTL/LAND/ESTOP issued mid-sequence got raced or duplicated by this
    sequence's own land command once it reached that point. Flips the fleet
    FSM to RTL partway through the dwell and asserts the sequence backs off
    without ever calling _land_one or _safe_recover itself - something else
    is already commanding this drone by then."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)

        land_calls = []
        original_land_one = manager._land_one
        async def tracking_land_one(drone_id):
            land_calls.append(drone_id)
            return await original_land_one(drone_id)
        manager._land_one = tracking_land_one

        recover_calls = []
        original_recover = manager._safe_recover
        async def tracking_recover(drone_id, drone_, tel_, context):
            recover_calls.append(context)
            await original_recover(drone_id, drone_, tel_, context)
        monkeypatch.setattr(manager, "_safe_recover", tracking_recover)

        target_alt = 0.3
        original_ingest = manager._ingest_bulk_telemetry
        state = {"first_reached_at": None}
        def declare_emergency_after_climb(snap, include_slow):
            original_ingest(snap, include_slow)
            tel = manager.telemetry[0]
            if abs(tel.get("local_z", 0.0) - target_alt) <= 0.2:
                if state["first_reached_at"] is None:
                    state["first_reached_at"] = time.monotonic()
                elif time.monotonic() - state["first_reached_at"] > 0.3:
                    manager.fleet_fsm._state = LifecycleState.RTL
        monkeypatch.setattr(manager, "_ingest_bulk_telemetry", declare_emergency_after_climb)

        with pytest.raises(HTTPException) as exc:
            await gcs_main.run_single_drone_sequence(
                gcs_main.SingleDroneSequenceRequest(target_altitude_m=target_alt, hover_duration_s=3.0),
                operator=commander,
            )
        assert exc.value.status_code == 500
        assert "yielding to fleet emergency state" in exc.value.detail
        assert land_calls == []  # never commanded its own landing
        assert recover_calls == []  # never raced the emergency handler's own recovery
    finally:
        manager.fleet_fsm._state = LifecycleState.STAGED
        await manager.shutdown()


@pytest.mark.asyncio
async def test_single_drone_sequence_cancellation_attempts_recovery(commander, patched_manager, monkeypatch):
    """B4: CancelledError is a BaseException, not an Exception, so cancelling
    the sequence used to skip every recovery attempt entirely and leave the
    vehicle flying. Cancels mid-dwell and asserts _safe_recover ran before
    the cancellation propagated."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)

        recover_calls = []
        original_recover = manager._safe_recover
        async def tracking_recover(drone_id, drone_, tel_, context):
            recover_calls.append(context)
            await original_recover(drone_id, drone_, tel_, context)
        monkeypatch.setattr(manager, "_safe_recover", tracking_recover)

        target_alt = 0.3
        original_ingest = manager._ingest_bulk_telemetry
        state = {"first_reached_at": None}
        def cancel_after_climb(snap, include_slow):
            original_ingest(snap, include_slow)
            tel = manager.telemetry[0]
            if abs(tel.get("local_z", 0.0) - target_alt) <= 0.2 and state["first_reached_at"] is None:
                state["first_reached_at"] = time.monotonic()
        monkeypatch.setattr(manager, "_ingest_bulk_telemetry", cancel_after_climb)

        task = asyncio.create_task(gcs_main.run_single_drone_sequence(
            gcs_main.SingleDroneSequenceRequest(target_altitude_m=target_alt, hover_duration_s=5.0),
            operator=commander,
        ))
        # Give it time to arm, climb and reach the dwell, then cancel mid-hold.
        while state["first_reached_at"] is None:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert recover_calls == ["after the sequence was cancelled"]
        assert 0 not in manager.single_drone_sequence_active  # guard released, as before
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_single_drone_unconfirmed_arm_attempts_recovery(commander, patched_manager, monkeypatch):
    """Missing-test item from the re-review: arm() can succeed without the
    vehicle ever being observed armed in telemetry (ack lost, etc.) - the
    sequence must still attempt a safe recovery rather than assuming nothing
    happened."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)

        original_ingest = manager._ingest_bulk_telemetry
        def never_armed(snap, include_slow):
            original_ingest(snap, include_slow)
            manager.telemetry[0]["armed"] = False
        monkeypatch.setattr(manager, "_ingest_bulk_telemetry", never_armed)

        recover_calls = []
        original_recover = manager._safe_recover
        async def tracking_recover(drone_id, drone_, tel_, context):
            recover_calls.append(context)
            await original_recover(drone_id, drone_, tel_, context)
        monkeypatch.setattr(manager, "_safe_recover", tracking_recover)

        with pytest.raises(HTTPException) as exc:
            await gcs_main.run_single_drone_sequence(
                gcs_main.SingleDroneSequenceRequest(target_altitude_m=0.3, hover_duration_s=1.0),
                operator=commander,
            )
        assert exc.value.status_code == 500
        assert "could not be confirmed" in exc.value.detail
        assert recover_calls == ["after an unconfirmed arm"]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_single_drone_arm_raising_attempts_recovery(commander, patched_manager, monkeypatch):
    """Missing-test item: arm() itself can raise without ruling out a
    partial arm (the command accepted but the ack lost). The sequence must
    still attempt a safe recovery rather than assuming it never armed."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)
        drone = manager.drones[0]

        async def rejected_arm():
            raise RuntimeError("ARM_DENIED")
        drone.action.arm = rejected_arm

        recover_calls = []
        original_recover = manager._safe_recover
        async def tracking_recover(drone_id, drone_, tel_, context):
            recover_calls.append(context)
            await original_recover(drone_id, drone_, tel_, context)
        monkeypatch.setattr(manager, "_safe_recover", tracking_recover)

        with pytest.raises(HTTPException) as exc:
            await gcs_main.run_single_drone_sequence(
                gcs_main.SingleDroneSequenceRequest(target_altitude_m=0.3, hover_duration_s=1.0),
                operator=commander,
            )
        assert exc.value.status_code == 500
        assert "Arming failed" in exc.value.detail
        assert recover_calls == ["after arm() raised"]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_single_drone_real_concurrent_invocation_is_refused(commander, patched_manager):
    """Missing-test item: the existing concurrency-guard test pre-populates
    single_drone_sequence_active by hand, which only proves the check exists,
    not that two real overlapping requests actually race correctly. Starts a
    genuine first request, lets it progress into its dwell, then fires a
    second real request concurrently and asserts it is refused while the
    first completes normally."""
    manager = patched_manager(DroneManager(ports=[14540], backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)

        first = asyncio.create_task(gcs_main.run_single_drone_sequence(
            gcs_main.SingleDroneSequenceRequest(target_altitude_m=0.3, hover_duration_s=1.0),
            operator=commander,
        ))
        # Let the first request actually add itself to the active set before
        # firing the second - the race this test exists to prove is real
        # concurrent overlap, not just "the set happens to be pre-populated".
        while 0 not in manager.single_drone_sequence_active:
            await asyncio.sleep(0.02)

        with pytest.raises(HTTPException) as exc:
            await gcs_main.run_single_drone_sequence(
                gcs_main.SingleDroneSequenceRequest(target_altitude_m=0.3, hover_duration_s=1.0),
                operator=commander,
            )
        assert exc.value.status_code == 409
        assert "already running" in exc.value.detail

        result = await first
        assert result["status"] == "COMPLETED"
        assert 0 not in manager.single_drone_sequence_active
    finally:
        await manager.shutdown()

