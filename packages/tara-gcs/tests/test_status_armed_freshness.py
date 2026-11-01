"""`armed` in /api/status and /api/sim/status is heartbeat-fresh (issue #171).

Observed on real PX4 SITL: after the PX4 process was killed mid-hover,
`/api/status` kept reporting `connected: 1, armed: 1`. `connected` was already
made heartbeat-fresh, but `armed` still counted the last value a dead link
left behind. These tests pin both counts, on both endpoints, to the same
freshness bar (`DroneManager.is_heartbeat_fresh`).
"""

import time

import pytest

from tara_gcs import main as gcs_main
from tara_gcs.auth import Operator, Role
from tara_gcs.drone_manager import DroneManager
from tara_gcs.sim_manager import SimManager

COMMANDER = Operator("test-commander", Role.FLIGHT_COMMANDER)


def _telemetry(manager, *, armed_fresh, armed_stale):
    """Drone 0 reports in; drone 1 is armed-but-silent (link lost mid-hover)."""
    now = time.time()
    stale = now - (manager.HEARTBEAT_STALE_S + 1.0)
    return {
        0: {"connected": True, "armed": armed_fresh, "last_heartbeat": now},
        # The transport flag is still set - exactly what a killed autopilot
        # with a not-yet-timed-out socket leaves behind.
        1: {"connected": True, "armed": armed_stale, "last_heartbeat": stale},
    }


@pytest.fixture
def manager(monkeypatch):
    manager = DroneManager(ports=[14540, 14541], backend="sim")
    monkeypatch.setattr(gcs_main, "drone_manager", manager)
    return manager


@pytest.mark.asyncio
async def test_status_armed_count_ignores_a_drone_with_a_stale_heartbeat(manager):
    manager.telemetry = _telemetry(manager, armed_fresh=True, armed_stale=True)

    status = await gcs_main.get_status()

    assert status["connected"] == 1
    assert status["armed"] == 1  # was 2: the dead link's last "armed" still counted


@pytest.mark.asyncio
async def test_status_armed_count_is_zero_when_the_only_armed_drone_went_silent(manager):
    """The exact SITL observation: one drone, armed, PX4 killed."""
    manager.telemetry = _telemetry(manager, armed_fresh=False, armed_stale=True)

    status = await gcs_main.get_status()

    assert (status["connected"], status["armed"]) == (1, 0)


@pytest.mark.asyncio
async def test_status_armed_count_still_counts_fresh_armed_drones(manager):
    manager.telemetry = _telemetry(manager, armed_fresh=True, armed_stale=False)

    assert (await gcs_main.get_status())["armed"] == 1


@pytest.mark.asyncio
async def test_sim_status_heartbeat_armed_count_ignores_a_stale_drone(manager, monkeypatch, tmp_path):
    monkeypatch.setattr(gcs_main, "sim_manager", SimManager(repo_root=tmp_path))
    manager.telemetry = _telemetry(manager, armed_fresh=True, armed_stale=True)

    heartbeat = (await gcs_main.sim_status(operator=COMMANDER))["heartbeat"]

    assert heartbeat["connected"] == 1
    assert heartbeat["armed"] == 1
