"""RF channel model: distance-based PER, Gilbert-Elliott burst loss, and
FleetState integration (issue #62).
"""

from __future__ import annotations

import numpy as np
import pytest

from tara_sim.fleet_state import FleetState, SimConfig
from tara_sim.rf_channel import RFChannelConfig, RFChannelModel


def run_ticks(fs: FleetState, n: int) -> None:
    for _ in range(n):
        fs.tick(fs.config.dt)


class TestRFChannelConfig:
    def test_enabled_false_by_default(self):
        assert RFChannelConfig().enabled is False

    def test_enabled_true_with_either_loss_term(self):
        assert RFChannelConfig(packet_loss_base=0.01).enabled is True
        assert RFChannelConfig(packet_loss_per_100m=0.01).enabled is True

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"packet_loss_base": -0.1},
            {"packet_loss_per_100m": -0.1},
            {"p_good_to_bad": -0.1},
            {"p_bad_to_good": -0.1},
        ],
    )
    def test_negative_rates_rejected(self, kwargs):
        with pytest.raises(ValueError):
            RFChannelConfig(**kwargs)

    def test_burst_loss_multiplier_below_one_rejected(self):
        with pytest.raises(ValueError):
            RFChannelConfig(burst_loss_multiplier=0.5)


class TestRFChannelModel:
    def test_drop_rate_zero_before_any_ticks(self):
        model = RFChannelModel(5, RFChannelConfig(packet_loss_base=0.5), np.random.default_rng(1))
        assert model.drop_rate() == pytest.approx(np.zeros(5))

    def test_zero_loss_config_never_drops(self):
        model = RFChannelModel(20, RFChannelConfig(), np.random.default_rng(1))
        distance_m = np.zeros(20)
        for _ in range(200):
            dropped = model.step(distance_m)
            assert not dropped.any()

    def test_full_loss_always_drops(self):
        model = RFChannelModel(20, RFChannelConfig(packet_loss_base=1.0), np.random.default_rng(1))
        distance_m = np.zeros(20)
        for _ in range(50):
            assert model.step(distance_m).all()

    def test_identical_seed_produces_identical_sequence(self):
        config = RFChannelConfig(packet_loss_base=0.2, packet_loss_per_100m=0.1)
        distance_m = np.full(30, 50.0)
        a = RFChannelModel(30, config, np.random.default_rng(42))
        b = RFChannelModel(30, config, np.random.default_rng(42))
        for _ in range(100):
            assert np.array_equal(a.step(distance_m), b.step(distance_m))

    def test_different_seeds_diverge(self):
        config = RFChannelConfig(packet_loss_base=0.3)
        distance_m = np.zeros(50)
        a = RFChannelModel(50, config, np.random.default_rng(1))
        b = RFChannelModel(50, config, np.random.default_rng(2))
        sequences_differ = False
        for _ in range(30):
            if not np.array_equal(a.step(distance_m), b.step(distance_m)):
                sequences_differ = True
        assert sequences_differ

    def test_empirical_drop_rate_matches_configured_base_rate(self):
        config = RFChannelConfig(packet_loss_base=0.2, p_good_to_bad=0.0)  # no bursts: isolate the base rate
        model = RFChannelModel(2000, config, np.random.default_rng(7))
        distance_m = np.zeros(2000)
        for _ in range(500):
            model.step(distance_m)
        assert model.drop_rate().mean() == pytest.approx(0.2, abs=0.02)

    def test_distance_based_attenuation_increases_loss_with_distance(self):
        config = RFChannelConfig(packet_loss_base=0.0, packet_loss_per_100m=0.2, p_good_to_bad=0.0)
        n = 1000
        model = RFChannelModel(2 * n, config, np.random.default_rng(3))
        # First half parked at the pad (0m); second half 250m out (0.5 PER).
        distance_m = np.concatenate([np.zeros(n), np.full(n, 250.0)])
        for _ in range(300):
            model.step(distance_m)
        rates = model.drop_rate()
        near_rate, far_rate = rates[:n].mean(), rates[n:].mean()
        assert near_rate == pytest.approx(0.0, abs=0.01)
        assert far_rate == pytest.approx(0.5, abs=0.03)
        assert far_rate > near_rate

    def test_burst_state_increases_loss_while_active(self):
        # Near-certain, near-permanent burst: isolate the BAD-state rate.
        config = RFChannelConfig(
            packet_loss_base=0.05, burst_loss_multiplier=10.0, p_good_to_bad=1.0, p_bad_to_good=0.0,
        )
        model = RFChannelModel(2000, config, np.random.default_rng(5))
        distance_m = np.zeros(2000)
        model.step(distance_m)  # first tick forces every drone into BAD state
        assert model.bad_state.all()
        for _ in range(50):
            model.step(distance_m)
        # 0.05 * 10.0 = 0.5, clipped
        assert model.drop_rate().mean() == pytest.approx(0.5, abs=0.03)


class TestFleetStateIntegration:
    def test_default_config_does_not_build_a_channel(self):
        fs = FleetState(5, SimConfig(rng_seed=1))
        assert fs.rf_channel is None

    def test_all_zero_rf_config_does_not_build_a_channel_either(self):
        fs = FleetState(5, SimConfig(rng_seed=1), rf_channel_config=RFChannelConfig())
        assert fs.rf_channel is None

    def test_rf_channel_and_dropout_together_rejected(self):
        with pytest.raises(ValueError):
            FleetState(
                5,
                SimConfig(rng_seed=1, dropout_prob_per_tick=0.1),
                rf_channel_config=RFChannelConfig(packet_loss_base=0.1),
            )

    def test_existing_dropout_behaviour_is_unaffected_by_the_new_parameter(self):
        """No regression: a caller who never touches rf_channel_config at all
        sees bit-for-bit the same connected sequence as before issue #62."""
        before = FleetState(10, SimConfig(rng_seed=9, dropout_prob_per_tick=0.3))
        after = FleetState(10, SimConfig(rng_seed=9, dropout_prob_per_tick=0.3), rf_channel_config=None)
        for _ in range(100):
            before.tick(before.config.dt)
            after.tick(after.config.dt)
            assert np.array_equal(before.connected, after.connected)

    def test_100_percent_loss_for_5_seconds_triggers_offboard_land_failsafe(self):
        """Issue #62 acceptance: "Simulating 100% downlink loss for 5 seconds
        triggers autonomous flight hold / RTL as expected." This fleet's
        existing offboard-loss failsafe (SimConfig.offboard_loss_timeout_s,
        default 1.0s) already lands a drone that stops hearing setpoints -
        the RF channel model only needs to correctly withhold them."""
        fs = FleetState(1, SimConfig(rng_seed=1), rf_channel_config=RFChannelConfig(packet_loss_base=1.0))
        fs.armed[:] = True
        fs.command_offboard_start(0)
        fs.command_setpoint(0, (0.0, 0.0, 5.0))
        assert not fs.offboard_failsafe[0]

        run_ticks(fs, int(5.0 / fs.config.dt))  # 5 simulated seconds of total loss

        assert fs.offboard_failsafe[0]
        assert fs.landing[0] or not fs.armed[0]  # landed and auto-disarmed, or still descending

    def test_10_percent_loss_degrades_telemetry_smoothly_without_crashing(self):
        """Issue #62 acceptance: "Simulating 10% packet drop degrades
        telemetry smoothly without crashing GCS." No GCS process in this
        test (tara_sim has no GCS dependency), so "without crashing" is
        checked at the layer this package owns: FleetState.tick() keeps
        running, connected flickers rather than latching permanently off,
        and the empirical drop rate matches the configured 10%."""
        n = 500
        fs = FleetState(
            n,
            SimConfig(rng_seed=11),
            rf_channel_config=RFChannelConfig(packet_loss_base=0.1, p_good_to_bad=0.0),
        )
        connected_true_count = 0
        total_samples = 0
        for _ in range(300):
            fs.tick(fs.config.dt)
            connected_true_count += int(fs.connected.sum())
            total_samples += n
        drop_rate = 1.0 - (connected_true_count / total_samples)
        assert drop_rate == pytest.approx(0.1, abs=0.02)
        # "Smoothly", not latched: both states are actually reachable.
        assert fs.connected.any()
        assert not fs.connected.all()
