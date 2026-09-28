"""API integration tests for the pre-arm interlocks (issue #48).

Requests go over real HTTP semantics through the FastAPI app (httpx2's ASGI
transport, so the app shares the test's event loop with the sim fleet) against
a real `DroneManager` on `backend="sim"` — the same no-mocks approach as
test_status_endpoint.py. The module-level `drone_manager` and
`preflight_engine` singletons are swapped for each test; the ASGI transport
does not run the app's startup hook, so the default px4 fleet never connects.
"""

from __future__ import annotations

import asyncio

import httpx2
import pytest

from tara_gcs import main as gcs_main
from tara_gcs.drone_manager import DroneManager
from tara_gcs.preflight import AuditLog, PreArmChecklistEngine

N = 3


@pytest.fixture
async def gcs(monkeypatch, tmp_path):
    """Returns ``setup(profile) -> (client, manager, engine)`` with a connected sim fleet."""
    managers = []
    clients = []

    async def setup(profile: str):
        manager = DroneManager(ports=list(range(N)), backend="sim", state_path=str(tmp_path / "state.json"))
        engine = PreArmChecklistEngine(profile, audit_log=AuditLog(str(tmp_path / "audit.jsonl")))
        monkeypatch.setattr(gcs_main, "drone_manager", manager)
        monkeypatch.setattr(gcs_main, "preflight_engine", engine)
        managers.append(manager)
        await manager.connect_all()
        await asyncio.sleep(1.0)  # let every monitor loop report in at least once
        client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=gcs_main.app), base_url="http://gcs")
        clients.append(client)
        return client, manager, engine

    yield setup
    for client in clients:
        await client.aclose()
    for manager in managers:
        await manager.shutdown()


def armed_ids(manager: DroneManager) -> list[int]:
    return sorted(d for d, tel in manager.telemetry.items() if tel["armed"])


async def wait_for_armed(manager: DroneManager, expected: list[int], timeout: float = 6.0) -> list[int]:
    """Polls until exactly ``expected`` are armed. Launch staggers drones by 1s
    within a batch, and armed state arrives on a stream, so a fixed sleep would
    either be flaky or needlessly long."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while armed_ids(manager) != expected and loop.time() < deadline:
        await asyncio.sleep(0.1)
    return armed_ids(manager)


@pytest.mark.asyncio
async def test_production_launch_is_412_and_nothing_arms(gcs):
    """A sim fleet has a plain 3D fix and no latch: PRODUCTION must refuse it outright."""
    client, manager, engine = await gcs("PRODUCTION")

    check = (await client.get("/api/preflight-check")).json()
    assert check["profile"] == "PRODUCTION"
    assert check["launch_permitted"] is False

    response = await client.post("/api/launch")
    assert response.status_code == 412
    detail = response.json()["detail"]
    assert detail["profile"] == "PRODUCTION"
    assert any("RTK_FIXED" in reason for reason in detail["blocking_reasons"])
    assert any("confirmation latch" in reason for reason in detail["blocking_reasons"])
    await asyncio.sleep(0.5)
    assert armed_ids(manager) == []
    assert engine.audit.recent[-1]["event"] == "launch_rejected"


@pytest.mark.asyncio
async def test_production_rejects_even_one_drone_below_battery_floor(gcs):
    client, manager, _ = await gcs("PRODUCTION")
    manager._sim_fleet.state.battery_pct[2] = 84.0
    await asyncio.sleep(0.7)

    response = await client.post("/api/launch")
    assert response.status_code == 412
    assert any(r.startswith("Drone 2: Battery state of charge") for r in response.json()["detail"]["blocking_reasons"])


@pytest.mark.asyncio
async def test_simulation_launch_succeeds_with_standard_gps_fix(gcs):
    client, manager, _ = await gcs("SIMULATION")
    assert all(tel["gps_fix_type"] == "FIX_3D" for tel in manager.telemetry.values())

    check = (await client.get("/api/preflight-check")).json()
    assert check["launch_permitted"] is True, check["blocking_reasons"]

    response = await client.post("/api/launch")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success" and body["drone_ids"] == [0, 1, 2]
    assert await wait_for_armed(manager, [0, 1, 2]) == [0, 1, 2]


@pytest.mark.asyncio
async def test_simulation_launches_only_the_green_drones(gcs):
    client, manager, _ = await gcs("SIMULATION")
    manager._sim_fleet.state.gps_fix_type[1] = "FIX_2D"
    await asyncio.sleep(0.7)

    response = await client.post("/api/launch")
    assert response.status_code == 200
    assert response.json()["drone_ids"] == [0, 2]
    assert response.json()["excluded_drone_ids"] == [1]
    assert await wait_for_armed(manager, [0, 2]) == [0, 2]


@pytest.mark.asyncio
async def test_engineering_arms_without_rtk_or_gnss(gcs):
    """Bench testing: no GNSS antenna at all, and the drone still arms."""
    client, manager, _ = await gcs("ENGINEERING")
    fs = manager._sim_fleet.state
    for i in range(N):
        fs.gps_fix_type[i] = "NO_GPS"
        fs.satellites[i] = 0
    await asyncio.sleep(0.7)

    response = await client.post("/api/launch")
    assert response.status_code == 200, response.json()
    assert await wait_for_armed(manager, [0, 1, 2]) == [0, 1, 2]


@pytest.mark.asyncio
async def test_engineering_ekf_override_is_two_step_and_audited(gcs, tmp_path):
    client, manager, _ = await gcs("ENGINEERING")
    manager._sim_fleet.state.is_armable = lambda drone_id: False
    await asyncio.sleep(0.7)
    assert (await client.post("/api/launch")).status_code == 412

    refused = await client.post(
        "/api/preflight/override/request", json={"rules": ["connected"], "reason": "x", "operator": "bob"}
    )
    assert refused.status_code == 400

    step1 = await client.post(
        "/api/preflight/override/request",
        json={"rules": ["ekf_health"], "reason": "bench, no GNSS", "operator": "bob"},
    )
    assert step1.status_code == 200
    token = step1.json()["token"]
    # Step 1 alone does not open the gate.
    assert (await client.post("/api/launch")).status_code == 412

    bad = await client.post("/api/preflight/override/confirm", json={"token": "wrong", "operator": "bob"})
    assert bad.status_code == 409
    step2 = await client.post("/api/preflight/override/confirm", json={"token": token, "operator": "bob"})
    assert step2.status_code == 200 and step2.json()["rules"] == ["ekf_health"]

    response = await client.post("/api/launch")
    assert response.status_code == 200
    assert response.json()["overridden_rules"] == ["ekf_health"]
    assert await wait_for_armed(manager, [0, 1, 2]) == [0, 1, 2]

    audit = (await client.get("/api/preflight/audit")).json()["entries"]
    events = [e["event"] for e in audit]
    for expected in ("override_refused", "override_requested", "override_confirm_rejected",
                     "override_confirmed", "launch_authorized"):
        assert expected in events
    assert (tmp_path / "audit.jsonl").read_text().count("override_confirmed") == 1


@pytest.mark.asyncio
async def test_latch_endpoints_drive_the_operator_latch_gate(gcs):
    client, _, _ = await gcs("PRODUCTION")

    def latch_gate(report):
        return next(g for g in report["fleet_gates"] if g["rule"] == "operator_latch")

    assert latch_gate((await client.get("/api/preflight-check")).json())["state"] == "fail"

    token = (await client.post("/api/preflight/latch/request", json={"operator": "alice"})).json()["token"]
    report = (await client.get("/api/preflight-check")).json()
    assert report["confirmations"]["latch"]["state"] == "pending"
    assert latch_gate(report)["state"] == "fail"

    assert (await client.post("/api/preflight/latch/confirm", json={"token": "nope"})).status_code == 409
    confirmed = await client.post("/api/preflight/latch/confirm", json={"token": token, "operator": "alice"})
    assert confirmed.status_code == 200

    report = (await client.get("/api/preflight-check")).json()
    assert latch_gate(report)["state"] == "pass"
    # The latch alone does not make a sim fleet production-ready.
    assert (await client.post("/api/launch")).status_code == 412

    audit = (await client.get("/api/preflight/audit", params={"limit": 3})).json()["entries"]
    assert len(audit) == 3
    confirmed_entry = next(e for e in audit if e["event"] == "latch_confirmed")
    assert confirmed_entry["gates"]["launch_permitted"] is False
