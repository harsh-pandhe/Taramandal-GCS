"""DroneManager's sim-backend RF channel wiring (issue #62): the
sim_rng_seed/sim_packet_loss_base/sim_packet_loss_per_100m/sim_tdma_jitter_ms
constructor kwargs reaching tara_sim.SimFleet, and rf_channel_drop_rate() reading it
back. /api/status's packet_drop_rate field (the same read path, through the
route) is covered in test_status_endpoint.py alongside its other fields.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

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


# --- TDMA slot jitter (issue #207) -------------------------------------------


@pytest.mark.asyncio
async def test_jitter_alone_reaches_the_sim_channel_config():
    """sim_tdma_jitter_ms > 0 builds an RF channel even with no packet loss configured."""
    manager = DroneManager(ports=list(range(20)), backend="sim", sim_rng_seed=1, sim_tdma_jitter_ms=1.25)
    try:
        await manager.connect_all()
        channel = manager._sim_fleet.state.rf_channel
        assert channel is not None
        assert channel.config.tdma_jitter_ms == 1.25
        assert channel.config.packet_loss_base == 0.0
        assert channel.config.packet_loss_per_100m == 0.0
        assert manager.rf_channel_drop_rate() == pytest.approx(0.0)  # no ticks yet
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_jitter_is_passed_together_with_packet_loss():
    manager = DroneManager(
        ports=list(range(20)), backend="sim", sim_rng_seed=1,
        sim_packet_loss_base=0.1, sim_packet_loss_per_100m=0.02, sim_tdma_jitter_ms=0.5,
    )
    try:
        await manager.connect_all()
        cfg = manager._sim_fleet.state.rf_channel.config
        assert (cfg.packet_loss_base, cfg.packet_loss_per_100m, cfg.tdma_jitter_ms) == (0.1, 0.02, 0.5)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_jitter_large_enough_to_collide_slots_drops_packets():
    """With a 500-drone slot guard of 1.52 ms, 10 ms of jitter must collide a visible share of slots."""
    manager = DroneManager(ports=list(range(500)), backend="sim", sim_rng_seed=3, sim_tdma_jitter_ms=10.0)
    try:
        await manager.connect_all()
        fs = manager._sim_fleet.state
        for _ in range(20):
            fs.tick(fs.config.dt)
        assert manager.rf_channel_drop_rate() > 0.05
        assert int(fs.rf_channel.packets_collided.sum()) > 0
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_zero_jitter_builds_no_channel_when_nothing_else_is_configured():
    manager = DroneManager(ports=list(range(5)), backend="sim", sim_tdma_jitter_ms=0.0)
    try:
        await manager.connect_all()
        assert manager._sim_fleet.state.rf_channel is None
        assert manager.rf_channel_drop_rate() is None
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_seeded_replay_is_identical_with_jitter_at_zero_and_without_the_option():
    """sim_tdma_jitter_ms=0.0 must not change one RNG draw of a seeded lossy run."""
    plain = DroneManager(ports=list(range(30)), backend="sim", sim_rng_seed=11, sim_packet_loss_base=0.3)
    zero = DroneManager(
        ports=list(range(30)), backend="sim", sim_rng_seed=11, sim_packet_loss_base=0.3, sim_tdma_jitter_ms=0.0,
    )
    try:
        await plain.connect_all()
        await zero.connect_all()
        fs_p, fs_z = plain._sim_fleet.state, zero._sim_fleet.state
        for _ in range(150):
            fs_p.tick(fs_p.config.dt)
            fs_z.tick(fs_z.config.dt)
            assert list(fs_p.connected) == list(fs_z.connected)
        assert list(fs_p.rf_channel.packets_dropped) == list(fs_z.rf_channel.packets_dropped)
        assert list(fs_p.rf_channel.packets_total) == list(fs_z.rf_channel.packets_total)
        assert int(fs_z.rf_channel.packets_collided.sum()) == 0
        assert plain.rf_channel_drop_rate() == zero.rf_channel_drop_rate()
    finally:
        await plain.shutdown()
        await zero.shutdown()


@pytest.mark.asyncio
async def test_seeded_replay_is_reproducible_with_jitter_on():
    a = DroneManager(ports=list(range(500)), backend="sim", sim_rng_seed=5, sim_tdma_jitter_ms=3.0)
    b = DroneManager(ports=list(range(500)), backend="sim", sim_rng_seed=5, sim_tdma_jitter_ms=3.0)
    try:
        await a.connect_all()
        await b.connect_all()
        fs_a, fs_b = a._sim_fleet.state, b._sim_fleet.state
        for _ in range(30):
            fs_a.tick(fs_a.config.dt)
            fs_b.tick(fs_b.config.dt)
            assert list(fs_a.connected) == list(fs_b.connected)
        assert list(fs_a.rf_channel.packets_collided) == list(fs_b.rf_channel.packets_collided)
    finally:
        await a.shutdown()
        await b.shutdown()


def _gcs_main_jitter(tmp_path, env_value: str | None) -> str:
    """What tara_gcs.main hands DroneManager for SIM_TDMA_JITTER_MS, read in a fresh interpreter.

    main.py builds its DroneManager at import time from the environment `tara up` sets, so
    the only honest check of that hop is to import it with the variable set.
    """
    repo = Path(__file__).resolve().parents[3]
    env = {
        **os.environ,
        "VEHICLE_BACKEND": "sim",
        "NUM_DRONES": "3",
        "GCS_STATE_PATH": str(tmp_path / "state.json"),
        "GCS_AUDIT_PATH": str(tmp_path / "audit.jsonl"),
        "PYTHONPATH": os.pathsep.join(
            str(repo / "packages" / pkg) for pkg in ("tara-core", "tara-gcs", "tara-sim")
        ),
    }
    env.pop("SIM_TDMA_JITTER_MS", None)
    if env_value is not None:
        env["SIM_TDMA_JITTER_MS"] = env_value
    out = subprocess.run(
        [sys.executable, "-c", "import tara_gcs.main as m; print(m.drone_manager._sim_tdma_jitter_ms)"],
        env=env, capture_output=True, text=True, timeout=120, cwd=tmp_path,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout.strip().splitlines()[-1]


def test_gcs_main_reads_sim_tdma_jitter_ms_from_the_environment(tmp_path):
    assert _gcs_main_jitter(tmp_path, "1.5") == "1.5"


def test_gcs_main_defaults_jitter_to_zero_when_the_variable_is_unset(tmp_path):
    assert _gcs_main_jitter(tmp_path, None) == "0.0"
