"""`build_services`'s env-var construction for `tara up`'s sim-backend RF
channel flags (issue #62) — unit-level, no supervisor/processes involved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tara_cli.config import TaraConfig
from tara_cli.services import UnsupportedBackendError, build_services


def _config(tmp_path: Path) -> TaraConfig:
    return TaraConfig(repo_root=tmp_path)


def _gcs_api_env(services) -> dict:
    (gcs_api,) = [s for s in services if s.name == "gcs-api"]
    return gcs_api.env


class TestSimRFChannelFlags:
    def test_defaults_set_no_sim_env_vars(self, tmp_path):
        env = _gcs_api_env(
            build_services(_config(tmp_path), num_drones=5, backend="sim", headless=True, rviz=False)
        )
        assert "SIM_RNG_SEED" not in env
        assert "SIM_PACKET_LOSS_BASE" not in env
        assert "SIM_PACKET_LOSS_PER_100M" not in env

    def test_seed_is_passed_through(self, tmp_path):
        env = _gcs_api_env(
            build_services(
                _config(tmp_path), num_drones=5, backend="sim", headless=True, rviz=False, sim_seed=42,
            )
        )
        assert env["SIM_RNG_SEED"] == "42"

    def test_seed_zero_is_passed_through_not_treated_as_unset(self, tmp_path):
        """0 is a legitimate seed — only None (the "not given" default) should
        be dropped, not any falsy-looking value."""
        env = _gcs_api_env(
            build_services(
                _config(tmp_path), num_drones=5, backend="sim", headless=True, rviz=False, sim_seed=0,
            )
        )
        assert env["SIM_RNG_SEED"] == "0"

    def test_packet_loss_flags_are_passed_through(self, tmp_path):
        env = _gcs_api_env(
            build_services(
                _config(tmp_path), num_drones=5, backend="sim", headless=True, rviz=False,
                sim_packet_loss=0.05, sim_packet_loss_per_100m=0.02,
            )
        )
        assert env["SIM_PACKET_LOSS_BASE"] == "0.05"
        assert env["SIM_PACKET_LOSS_PER_100M"] == "0.02"

    def test_px4_backend_ignores_sim_flags(self, tmp_path):
        """These flags are sim-only (see the issue and both docstrings) — a
        px4 run passing them must not get any SIM_* env var, since nothing
        on that backend would read it."""
        config = _config(tmp_path)
        config.px4_autopilot_dir = str(tmp_path)  # build_services doesn't check this exists
        env = _gcs_api_env(
            build_services(
                config, num_drones=3, backend="px4", headless=True, rviz=False,
                sim_seed=42, sim_packet_loss=0.1, sim_packet_loss_per_100m=0.1,
            )
        )
        assert "SIM_RNG_SEED" not in env
        assert "SIM_PACKET_LOSS_BASE" not in env
        assert "SIM_PACKET_LOSS_PER_100M" not in env

    def test_unknown_backend_still_rejected_with_sim_flags_present(self, tmp_path):
        with pytest.raises(UnsupportedBackendError):
            build_services(
                _config(tmp_path), num_drones=3, backend="bogus", headless=True, rviz=False, sim_seed=1,
            )
