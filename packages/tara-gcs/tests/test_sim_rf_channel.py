"""DroneManager's sim-backend RF channel wiring (issue #62): the
sim_rng_seed/sim_packet_loss_base/sim_packet_loss_per_100m constructor
kwargs reaching tara_sim.SimFleet, and rf_channel_drop_rate() reading it
back. /api/status's packet_drop_rate field (the same read path, through the
route) is covered in test_status_endpoint.py alongside its other fields.
"""

from __future__ import annotations

import pytest

from tara_gcs.drone_manager import DroneManager


@pytest.mark.asyncio
async def test_no_loss_configured_reports_none():
    manager = DroneManager(ports=list(range(3)), backend="sim")
    try:
        await manager.connect_all()
        assert manager.rf_channel_drop_rate() is None
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_before_connect_all_reports_none():
    manager = DroneManager(ports=list(range(3)), backend="sim", sim_packet_loss_base=0.1)
    assert manager.rf_channel_drop_rate() is None


@pytest.mark.asyncio
async def test_px4_backend_reports_none_even_with_sim_kwargs_set():
    """sim_packet_loss_base etc. are no-ops on backend="px4" — nothing reads
    them there, so there is never an RF channel to report on."""
    manager = DroneManager(ports=[14540], backend="px4", sim_packet_loss_base=0.1)
    assert manager.rf_channel_drop_rate() is None


@pytest.mark.asyncio
async def test_configured_loss_builds_a_channel_and_reports_a_rate():
    manager = DroneManager(
        ports=list(range(50)), backend="sim", sim_rng_seed=1, sim_packet_loss_base=0.2,
    )
    try:
        await manager.connect_all()
        assert manager.rf_channel_drop_rate() == pytest.approx(0.0)  # no ticks yet

        fs = manager._sim_fleet.state
        for _ in range(200):
            fs.tick(fs.config.dt)

        rate = manager.rf_channel_drop_rate()
        assert rate is not None
        # A sanity range, not a tight statistical pin - see the equivalent
        # comment in test_status_endpoint.py's version of this check for why
        # (default Gilbert-Elliott bursts inflate the mean above the 0.2 base
        # rate a little; the exact math is pinned in tara-sim's own suite).
        assert 0.15 < rate < 0.3
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_seed_makes_the_drop_sequence_reproducible():
    a = DroneManager(ports=list(range(20)), backend="sim", sim_rng_seed=7, sim_packet_loss_base=0.3)
    b = DroneManager(ports=list(range(20)), backend="sim", sim_rng_seed=7, sim_packet_loss_base=0.3)
    try:
        await a.connect_all()
        await b.connect_all()
        fs_a, fs_b = a._sim_fleet.state, b._sim_fleet.state
        for _ in range(100):
            fs_a.tick(fs_a.config.dt)
            fs_b.tick(fs_b.config.dt)
            assert list(fs_a.connected) == list(fs_b.connected)
    finally:
        await a.shutdown()
        await b.shutdown()
