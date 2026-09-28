"""Tests for GET /api/status — the fleet health summary `tara status` reads.

Calls the route function directly rather than through an HTTP client (no
test-only httpx dependency needed — same reasoning as
`tara-studio/tests/test_assign_chain.py`), against a real `DroneManager` on
`backend="sim"` rather than a mock. The counts under test are derived from the
telemetry dict the monitor loops populate, so a fake manager would only prove
that `sum` works; the sim backend actually exercises the fields being counted.
"""

import asyncio

import pytest

from tara_gcs import main as gcs_main
from tara_gcs.drone_manager import DroneManager


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
