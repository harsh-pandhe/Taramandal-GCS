"""Tests for the /api/sim/* and /api/rviz/* routes.

Calls the route functions directly rather than through an HTTP client, the
same way test_status_endpoint.py does — no test-only httpx dependency, and the
routes are thin enough that what's worth asserting is the status-code mapping
and the shape handed to the dashboard, not FastAPI's routing.

Process lifecycle itself is covered in test_sim_manager.py; these tests use a
manager pointed at a tmp repo so nothing here spawns PX4 or Gazebo.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from tara_gcs import main as gcs_main
from tara_gcs.auth import Operator, Role
from tara_gcs.drone_manager import DroneManager
from tara_gcs.sim_manager import SimManager


# Issue #64: route handlers called directly take the operator that the auth
# dependency would otherwise inject.
COMMANDER = Operator("test-commander", Role.FLIGHT_COMMANDER)


@pytest.fixture
def sim(monkeypatch, tmp_path):
    """Redirect the module-level sim_manager singleton at a throwaway repo root.

    `tara_gcs.main` builds one at import time and the routes close over it, so
    tests swap it out rather than constructing their own app.
    """
    manager = SimManager(repo_root=tmp_path)
    monkeypatch.setattr(gcs_main, "sim_manager", manager)
    return manager


@pytest.mark.asyncio
async def test_launch_sim_backend_reports_running(sim):
    """A sim-backend launch succeeds without spawning anything."""
    res = await gcs_main.sim_launch(
        gcs_main.SimLaunchRequest(num_drones=100, backend="sim"), operator=COMMANDER
    )

    assert res["status"] == "success"
    assert res["sim"]["running"] is True
    assert res["sim"]["num_drones"] == 100


@pytest.mark.asyncio
async def test_launch_rejects_oversized_px4_fleet_as_400(sim):
    """500 real SITL instances is a bad request, not a server error."""
    with pytest.raises(HTTPException) as exc:
        await gcs_main.sim_launch(
            gcs_main.SimLaunchRequest(num_drones=500, backend="px4"), operator=COMMANDER
        )

    assert exc.value.status_code == 400
    assert "real-SITL ceiling" in exc.value.detail


@pytest.mark.asyncio
async def test_double_launch_is_a_409_not_a_400(sim):
    """Clicking Launch twice is a conflict — distinguishable in the UI from a
    malformed request, which is why the manager's error is mapped by content."""
    await gcs_main.sim_launch(gcs_main.SimLaunchRequest(num_drones=5, backend="sim"), operator=COMMANDER)

    with pytest.raises(HTTPException) as exc:
        await gcs_main.sim_launch(gcs_main.SimLaunchRequest(num_drones=5, backend="sim"), operator=COMMANDER)

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_status_merges_process_view_with_fleet_heartbeat(sim, monkeypatch):
    """The dashboard's pills need processes and fleet health from one call."""
    manager = DroneManager(ports=[14540, 14541], backend="sim")
    monkeypatch.setattr(gcs_main, "drone_manager", manager)
    manager.telemetry = {
        0: {"connected": True, "armed": True, "home_lat": 47.397, "home_lon": 8.545},
        1: {"connected": True, "armed": False, "home_lat": 0.0, "home_lon": 0.0},
    }

    status = await gcs_main.sim_status(operator=COMMANDER)

    assert status["heartbeat"]["alive"] is True
    assert status["heartbeat"]["connected"] == 2
    assert status["heartbeat"]["armed"] == 1
    assert status["heartbeat"]["expected"] == 2
    # Only drone 0 has a real home fix; drone 1's 0.0/0.0 is "not yet locked",
    # the exact state the home-lock fix exists to make visible.
    assert status["heartbeat"]["home_locked"] == 1


@pytest.mark.asyncio
async def test_status_with_no_fleet_reports_not_alive(sim, monkeypatch):
    """Processes up but nothing reporting in is a half-started stack, and the
    operator needs to see that before pressing Launch."""
    monkeypatch.setattr(gcs_main, "drone_manager", DroneManager(ports=[], backend="sim"))

    status = await gcs_main.sim_status(operator=COMMANDER)

    assert status["heartbeat"]["alive"] is False
    assert status["heartbeat"]["connected"] == 0


@pytest.mark.asyncio
async def test_stop_is_idempotent_from_the_api(sim):
    """Terminate is always clickable; stopping nothing must still be a success."""
    res = await gcs_main.sim_stop(operator=COMMANDER)

    assert res["status"] == "success"
    assert res["stopped"] == []
    assert res["sim"]["running"] is False


@pytest.mark.asyncio
async def test_rviz_without_a_fleet_is_a_400(sim):
    with pytest.raises(HTTPException) as exc:
        await gcs_main.rviz_launch(gcs_main.RvizLaunchRequest(), operator=COMMANDER)

    assert exc.value.status_code == 400
    assert "no PX4 fleet is running" in exc.value.detail
