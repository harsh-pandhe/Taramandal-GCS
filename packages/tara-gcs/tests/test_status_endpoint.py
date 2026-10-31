"""Tests for GET /api/status — the fleet health summary `tara status` reads.

Calls the route function directly rather than through an HTTP client (no
test-only httpx dependency needed — same reasoning as
`tara-studio/tests/test_assign_chain.py`), against a real `DroneManager` on
`backend="sim"` rather than a mock. The counts under test are derived from the
telemetry dict the monitor loops populate, so a fake manager would only prove
that `sum` works; the sim backend actually exercises the fields being counted.
"""

import asyncio
import json
import time

import pytest

from tara_gcs import main as gcs_main
from tara_gcs.drone_manager import DroneManager, _pad_offset


@pytest.fixture
def patched_manager(monkeypatch):
    """Swap in a caller-supplied DroneManager for the module-level singleton.

    `tara_gcs.main` builds one `drone_manager` at import time from the
    environment, and the route closes over that module global. Tests need
    their own fleet, so the global is redirected for the duration of each
    test and restored after.
    """

    def _install(manager: DroneManager) -> DroneManager:
        monkeypatch.setattr(gcs_main, "drone_manager", manager)
        return manager

    return _install


@pytest.mark.asyncio
async def test_status_reports_fleet_counts_from_live_telemetry(patched_manager):
    """A connected sim fleet reports its size, backend, and connected count."""
    n = 4
    manager = patched_manager(DroneManager(ports=list(range(n)), backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)  # let the connection monitors report in

        status = await gcs_main.get_status()

        assert status["backend"] == "sim"
        assert status["fidelity"] == "kinematic"
        assert status["num_drones"] == n
        assert status["connected"] == n
        assert status["armed"] == 0  # nothing launched yet
        assert status["trajectory_running"] is False
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_status_armed_count_tracks_the_launch_sequence(patched_manager):
    """Arming the fleet moves the armed count off zero.

    This is the field a load-test script would gate on ("is the fleet actually
    flying yet"), so it is worth proving it follows a real launch rather than
    staying pinned at its initial value.
    """
    n = 2
    manager = patched_manager(DroneManager(ports=list(range(n)), backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)
        assert (await gcs_main.get_status())["armed"] == 0

        # Arming is gated on every drone holding a verified mission (#47).
        show = {}
        for i in range(n):
            x, y = _pad_offset(i)
            show[i] = [{"time": 0.0, "x": x, "y": y, "z": 0.0}, {"time": 1.0, "x": x, "y": y, "z": -2.0}]
        await manager.upload_show(show)
        await manager.launch_sequence()
        await asyncio.sleep(0.5)  # armed-state monitors are a stream, not instant

        status = await gcs_main.get_status()
        assert status["armed"] == n
        assert status["connected"] == n
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_status_answers_before_any_drone_connects(patched_manager):
    """The endpoint must not require a live fleet to answer.

    `tara status` probes this route precisely when the stack may be unhealthy,
    so a fleet that never connected has to produce zeroes rather than an
    exception — otherwise the CLI could not tell "backend down" from "backend
    up, fleet dead".
    """
    patched_manager(DroneManager(ports=[14540, 14541], backend="sim"))

    status = await gcs_main.get_status()

    assert status["num_drones"] == 2
    assert status["connected"] == 0
    assert status["armed"] == 0


@pytest.mark.asyncio
async def test_status_connected_count_requires_a_fresh_heartbeat_not_just_the_flag(patched_manager):
    """A frozen autopilot that kept its transport session open must not
    count as "connected" (self-review, not a review-thread finding): the
    connected count used to check only the transport-level `connected` flag,
    so a drone whose heartbeats had stopped arriving - but whose MAVSDK/UDP
    session hadn't been torn down - would read as connected here forever,
    even though every actual flight-command path already refuses it as
    stale.
    """
    n = 2
    manager = patched_manager(DroneManager(ports=list(range(n)), backend="sim"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)
        assert (await gcs_main.get_status())["connected"] == n

        # Freeze one drone's heartbeat without touching its connection flag -
        # exactly the state a hung autopilot with a live socket leaves behind.
        manager.telemetry[0]["last_heartbeat"] = time.time() - (manager.HEARTBEAT_STALE_S + 1.0)
        assert manager.telemetry[0]["connected"] is True

        status = await gcs_main.get_status()
        assert status["connected"] == n - 1
    finally:
        await manager.shutdown()


def test_fleet_lifecycle_audit_entries_carry_backend_and_fidelity(patched_manager, tmp_path):
    """Issue #170: the fleet lifecycle trail is the one audit trail that
    predates the context-provider pattern command_audit/preflight already
    use (test_command_and_preflight_audit_entries_carry_backend_and_fidelity,
    below) - verifies the same wiring reaches it too, through its owning
    DroneManager rather than the module-level drone_manager singleton
    (fleet_fsm's context closes over `self`, not a monkeypatched global).

    Checks the written audit file, not apply()'s return value: the context
    is merged only into the entry _audit() writes to disk, not into the
    in-memory record history/API responses are built from - the issue scopes
    this to the evidence-pack trail, which is the disk log.
    """
    from tara_gcs.fleet_fsm import Command

    for backend, fidelity in (("sim", "kinematic"), ("px4", "px4-sitl")):
        audit_path = tmp_path / f"{backend}_fleet_audit.jsonl"
        manager = patched_manager(DroneManager(ports=[0], backend=backend, audit_path=str(audit_path)))
        manager.fleet_fsm.apply(Command.LAUNCH, operator="tester", trigger="test")
        (entry,) = [json.loads(line) for line in audit_path.read_text().splitlines()]
        assert entry["backend"] == backend
        assert entry["fidelity"] == fidelity
        assert entry["operator"] == "tester"


@pytest.mark.asyncio
async def test_sim_inject_fault_route_reaches_the_manager(patched_manager):
    """Issue #63: the HTTP layer itself - request parsing, the Commander
    role gate, error translation - not DroneManager.inject_fault's own
    behaviour (covered thoroughly in test_fault_injection.py)."""
    from tara_gcs.auth import Operator, Role

    manager = patched_manager(DroneManager(ports=list(range(2)), backend="sim"))
    try:
        await manager.connect_all()
        req = gcs_main.InjectFaultRequest(drone_id=0, fault_type="battery_drop", params={"pct": 5.0})
        result = await gcs_main.sim_inject_fault(req, operator=Operator("tester", Role.FLIGHT_COMMANDER))
        assert result["status"] == "success"
        assert manager._sim_fleet.state.battery_pct[0] == pytest.approx(5.0)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_sim_inject_fault_route_translates_errors_to_http_status(patched_manager):
    from fastapi import HTTPException
    from tara_gcs.auth import Operator, Role

    manager = patched_manager(DroneManager(ports=[0], backend="sim"))
    try:
        await manager.connect_all()
        operator = Operator("tester", Role.FLIGHT_COMMANDER)

        with pytest.raises(HTTPException) as exc_info:
            await gcs_main.sim_inject_fault(
                gcs_main.InjectFaultRequest(drone_id=0, fault_type="not_a_real_fault", params={}),
                operator=operator,
            )
        assert exc_info.value.status_code == 400

        with pytest.raises(HTTPException) as exc_info:
            await gcs_main.sim_inject_fault(
                gcs_main.InjectFaultRequest(drone_id=99, fault_type="battery_drop", params={"pct": 1.0}),
                operator=operator,
            )
        assert exc_info.value.status_code == 404
    finally:
        await manager.shutdown()


def test_every_backend_has_a_fidelity_tier():
    """Issue #170: the evidence tier is derived from the backend, never defaulted.

    A kinematic (tara_sim) pass is scale evidence only; PX4 SITL is the tier
    with real flight dynamics. An unlabelled or mislabelled backend would let
    one be quoted as the other, so every accepted backend must map to a tier.
    """
    from tara_gcs.drone_manager import FIDELITY_BY_BACKEND

    assert FIDELITY_BY_BACKEND == {"sim": "kinematic", "px4": "px4-sitl"}
    assert DroneManager(ports=[0], backend="sim").fidelity == "kinematic"
    assert DroneManager(ports=[0], backend="px4").fidelity == "px4-sitl"


@pytest.mark.asyncio
async def test_status_packet_drop_rate_is_null_when_no_rf_loss_configured(patched_manager):
    """Issue #62: packet_drop_rate must be null, not 0.0, when the sim fleet
    has no RF channel model at all - distinct from "connected with zero loss
    observed so far", which IS 0.0 (the next test)."""
    manager = patched_manager(DroneManager(ports=list(range(3)), backend="sim"))
    try:
        await manager.connect_all()
        status = await gcs_main.get_status()
        assert status["packet_drop_rate"] is None
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_status_packet_drop_rate_reflects_the_rf_channel_model(patched_manager):
    manager = patched_manager(
        DroneManager(ports=list(range(30)), backend="sim", sim_rng_seed=3, sim_packet_loss_base=0.25)
    )
    try:
        await manager.connect_all()
        assert (await gcs_main.get_status())["packet_drop_rate"] == pytest.approx(0.0)

        fs = manager._sim_fleet.state
        for _ in range(200):
            fs.tick(fs.config.dt)

        status = await gcs_main.get_status()
        # A sanity range, not a tight pin: the default Gilbert-Elliott burst
        # parameters push the empirical rate a bit above the 0.25 base rate
        # (bursts are rarer but ~4x as lossy while active) - the exact burst
        # math is pinned precisely in tara-sim's own test suite
        # (test_rf_channel.py); this integration test's job is only proving
        # the wiring from DroneManager's constructor kwargs through to this
        # endpoint actually reaches tara_sim, not re-deriving its statistics.
        assert 0.2 < status["packet_drop_rate"] < 0.35
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_status_packet_drop_rate_is_null_on_px4_backend(patched_manager):
    patched_manager(DroneManager(ports=[14540], backend="px4"))
    status = await gcs_main.get_status()
    assert status["packet_drop_rate"] is None


@pytest.mark.asyncio
async def test_status_reports_px4_fidelity_without_connecting(patched_manager):
    """/api/status labels a px4 backend as px4-sitl even before any drone connects."""
    patched_manager(DroneManager(ports=[14540], backend="px4"))
    status = await gcs_main.get_status()
    assert status["backend"] == "px4"
    assert status["fidelity"] == "px4-sitl"


@pytest.mark.asyncio
async def test_api_mode_reports_fidelity(patched_manager):
    """Issue #170: /api/mode carried backend but not fidelity - the one
    status-style endpoint still missing it, so the dashboard header and
    single-drone screen (both read from here, not /api/status) had nothing
    to show.
    """
    from tara_gcs.auth import Operator, Role

    patched_manager(DroneManager(ports=[14540], backend="px4"))
    mode_info = await gcs_main.get_mode(operator=Operator("tester", Role.VIEWER))
    assert mode_info["backend"] == "px4"
    assert mode_info["fidelity"] == "px4-sitl"


def test_command_and_preflight_audit_entries_carry_backend_and_fidelity(patched_manager):
    """Issue #170: every audit entry from the GCS names the backend it ran on.

    Both module-level trails (operator commands, pre-arm decisions) are stamped
    through the same provider, read at record time, so swapping the backend
    changes the label on the very next entry rather than being frozen at import.
    """
    for backend, fidelity in (("sim", "kinematic"), ("px4", "px4-sitl")):
        patched_manager(DroneManager(ports=[0], backend=backend))
        for log in (gcs_main.command_audit, gcs_main.preflight_engine.audit):
            entry = log.record("test_event", operator="tester")
            assert entry["backend"] == backend
            assert entry["fidelity"] == fidelity
            assert entry["operator"] == "tester"
