"""RF channel model: distance-based PER, Gilbert-Elliott burst loss, and
FleetState integration (issue #62); TDMA slot jitter (issue #207).
"""

from __future__ import annotations

import numpy as np
import pytest

from tara_sim.fleet_state import FleetState, SimConfig
from tara_sim.rf_channel import UPLINK_EPOCH_MS, UPLINK_FRAME_MS, RFChannelConfig, RFChannelModel, uplink_guard_ms


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


class TestTDMASlotJitter:
    """TDMA slot jitter and inter-slot collisions (issue #207)."""

    def test_jitter_is_off_by_default(self):
        cfg = RFChannelConfig()
        assert cfg.tdma_jitter_ms == 0.0
        assert cfg.tdma_guard_ms is None
        assert not cfg.enabled

    def test_jitter_alone_enables_the_model(self):
        assert RFChannelConfig(tdma_jitter_ms=0.5).enabled

    @pytest.mark.parametrize("value", [-0.1, -1.0, float("nan"), float("inf"), float("-inf")])
    def test_jitter_must_be_finite_and_not_negative(self, value):
        with pytest.raises(ValueError, match="tdma_jitter_ms"):
            RFChannelConfig(tdma_jitter_ms=value)

    @pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf")])
    def test_an_explicit_guard_must_be_finite_and_positive(self, value):
        with pytest.raises(ValueError, match="tdma_guard_ms"):
            RFChannelConfig(tdma_jitter_ms=1.0, tdma_guard_ms=value)

    def test_guard_follows_the_gcs_uplink_slot_geometry(self):
        assert uplink_guard_ms(500) == pytest.approx(1.52)  # (4 ms slot - 0.96 ms frame) / 2
        assert uplink_guard_ms(1000) == pytest.approx(0.52)
        assert uplink_guard_ms(2083) > 0.0
        assert uplink_guard_ms(2084) <= 0.0
        model = RFChannelModel(500, RFChannelConfig(tdma_jitter_ms=1.0), np.random.default_rng(0))
        assert model.tdma_guard_ms == pytest.approx(1.52)
        override = RFChannelModel(500, RFChannelConfig(tdma_jitter_ms=1.0, tdma_guard_ms=0.3), np.random.default_rng(0))
        assert override.tdma_guard_ms == 0.3

    def test_copied_geometry_matches_the_gcs_scheduler(self):
        sched = pytest.importorskip("tara_gcs.tdma_scheduler")
        assert UPLINK_EPOCH_MS == sched.EPOCH_SECONDS * 1000.0
        assert UPLINK_FRAME_MS == pytest.approx(sched.TDMAScheduler(500).transmission_time_s() * 1000.0)
        assert uplink_guard_ms(500) * 2 + UPLINK_FRAME_MS == pytest.approx(sched.TDMAScheduler(500).slot_width_s * 1000.0)

    def test_no_guard_left_refuses_jitter_but_not_a_jitter_free_channel(self):
        with pytest.raises(ValueError, match="no TDMA guard time left at 2084 drones"):
            RFChannelModel(2084, RFChannelConfig(tdma_jitter_ms=0.1), np.random.default_rng(0))
        RFChannelModel(2084, RFChannelConfig(packet_loss_base=0.1), np.random.default_rng(0))
        RFChannelModel(2084, RFChannelConfig(tdma_jitter_ms=0.1, tdma_guard_ms=0.2), np.random.default_rng(0))

    def _collisions(self, offsets, guard=1.0):
        model = RFChannelModel(len(offsets), RFChannelConfig(tdma_jitter_ms=1.0, tdma_guard_ms=guard), np.random.default_rng(0))
        return model._slot_collisions(np.array(offsets, dtype=float)).tolist()

    def test_within_the_guard_nothing_collides(self):
        assert self._collisions([0.0, 0.99, -0.99, 1.0, -1.0]) == [False] * 5

    def test_a_late_send_takes_out_its_own_and_the_next_slot(self):
        assert self._collisions([0.0, 0.0, 1.5, 0.0, 0.0]) == [False, False, True, True, False]

    def test_an_early_send_takes_out_its_own_and_the_previous_slot(self):
        assert self._collisions([0.0, 0.0, -1.5, 0.0, 0.0]) == [False, True, True, False, False]

    def test_slots_wrap_around_the_epoch(self):
        assert self._collisions([-1.5, 0.0, 0.0, 0.0]) == [True, False, False, True]
        assert self._collisions([0.0, 0.0, 0.0, 1.5]) == [True, False, False, True]

    def test_a_lone_drone_never_collides(self):
        assert self._collisions([5.0]) == [False]

    def test_empirical_loss_matches_the_collision_probability(self):
        """At 500 drones (guard 1.52 ms) and 1 ms jitter, a drone's packet is lost when its own send
        leaves the guard, or its previous neighbour is late, or its next one is early."""
        from math import erf, sqrt

        n, jitter, ticks = 500, 1.0, 400
        model = RFChannelModel(n, RFChannelConfig(tdma_jitter_ms=jitter), np.random.default_rng(3))
        lost = sum(int(model.step(np.zeros(n)).sum()) for _ in range(ticks))
        p_out = 1.0 - erf(uplink_guard_ms(n) / jitter / sqrt(2.0))  # P(|offset| > guard)
        expected = 1.0 - (1.0 - p_out) * (1.0 - p_out / 2) ** 2
        assert lost / (n * ticks) == pytest.approx(expected, abs=0.01)
        assert lost == model.packets_dropped.sum()
        assert np.array_equal(model.packets_collided, model.packets_dropped)  # no other loss term is on

    def test_more_jitter_loses_more_packets(self):
        rates = []
        for jitter in (0.25, 0.5, 1.0, 2.0):
            model = RFChannelModel(500, RFChannelConfig(tdma_jitter_ms=jitter), np.random.default_rng(5))
            for _ in range(100):
                model.step(np.zeros(500))
            rates.append(float(model.drop_rate().mean()))
        assert rates == sorted(rates)
        assert rates[0] < 0.001 < rates[-1]

    def test_identical_seed_produces_identical_collisions(self):
        cfg = RFChannelConfig(packet_loss_base=0.05, tdma_jitter_ms=1.0)
        a = RFChannelModel(200, cfg, np.random.default_rng(42))
        b = RFChannelModel(200, cfg, np.random.default_rng(42))
        for _ in range(50):
            assert np.array_equal(a.step(np.zeros(200)), b.step(np.zeros(200)))

    def test_without_jitter_the_drop_sequence_is_exactly_the_pre_207_one(self):
        """Jitter off draws nothing extra: the model consumes the same three uniform draws per tick
        as before #207, so a seeded run replays bit for bit."""
        n, cfg = 50, RFChannelConfig(packet_loss_base=0.2, packet_loss_per_100m=0.1)
        model = RFChannelModel(n, cfg, np.random.default_rng(7))
        ref_rng, bad = np.random.default_rng(7), np.zeros(n, dtype=bool)
        dist = np.linspace(0.0, 150.0, n)
        for _ in range(100):
            to_bad = ~bad & (ref_rng.random(n) < cfg.p_good_to_bad)
            to_good = bad & (ref_rng.random(n) < cfg.p_bad_to_good)
            bad[to_bad], bad[to_good] = True, False
            base = np.clip(cfg.packet_loss_base + cfg.packet_loss_per_100m * dist / 100.0, 0.0, 1.0)
            per = np.where(bad, np.clip(base * cfg.burst_loss_multiplier, 0.0, 1.0), base)
            assert np.array_equal(model.step(dist), ref_rng.random(n) < per)

    def test_jitter_off_never_counts_a_collision(self):
        model = RFChannelModel(500, RFChannelConfig(packet_loss_base=0.3), np.random.default_rng(1))
        for _ in range(50):
            model.step(np.zeros(500))
        assert model.packets_dropped.sum() > 0
        assert not model.packets_collided.any()

    def test_collisions_add_to_the_other_loss_terms(self):
        """Same seed, one tick each: the jitter draws come after the loss rolls, so the loss terms
        drop exactly the same packets and collisions only add to them. (Over several ticks the
        shared generator diverges, so the comparison is per seed.)"""
        n, extra = 500, 0
        for seed in range(20):
            plain = RFChannelModel(n, RFChannelConfig(packet_loss_base=0.1), np.random.default_rng(seed))
            jittered = RFChannelModel(n, RFChannelConfig(packet_loss_base=0.1, tdma_jitter_ms=1.0), np.random.default_rng(seed))
            a, b = plain.step(np.zeros(n)), jittered.step(np.zeros(n))
            assert not (a & ~b).any()  # every packet the loss terms drop is still dropped
            assert np.array_equal(b, a | (jittered.packets_collided > 0))
            extra += int((b & ~a).sum())
        assert extra > 0

    def test_fleet_state_builds_a_channel_for_jitter_alone(self):
        fs = FleetState(1000, SimConfig(rng_seed=1), rf_channel_config=RFChannelConfig(tdma_jitter_ms=1.0))  # guard 0.52 ms
        assert fs.rf_channel is not None
        run_ticks(fs, 50)
        assert fs.rf_channel.packets_collided.sum() > 0

    def test_fleet_state_rejects_jitter_together_with_flat_dropout(self):
        with pytest.raises(ValueError):
            FleetState(5, SimConfig(rng_seed=1, dropout_prob_per_tick=0.1), rf_channel_config=RFChannelConfig(tdma_jitter_ms=1.0))
