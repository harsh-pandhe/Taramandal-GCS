"""Physics invariants for the vectorized fleet simulator.

No mocks, no asyncio — `FleetState.tick()` is plain numpy, so it's driven
directly and deterministically (following the parametrized, mock-free style of
`tara_core`'s own tests).
"""

import numpy as np
import pytest

from tara_sim.fleet_state import FleetState, SimConfig


def run_ticks(fs: FleetState, n: int) -> None:
    for _ in range(n):
        fs.tick(fs.config.dt)


class TestSetpointTracking:
    def test_converges_to_a_fixed_setpoint(self):
        fs = FleetState(1, SimConfig(rng_seed=1, wind_std_ms2=0.0))
        fs.armed[:] = True
        fs.command_setpoint(0, (3.0, -2.0, 5.0))
        run_ticks(fs, 500)
        assert fs.position[0] == pytest.approx([3.0, -2.0, 5.0], abs=0.15)

    def test_disarmed_drone_does_not_move(self):
        fs = FleetState(1, SimConfig(rng_seed=1))
        fs.command_setpoint(0, (10.0, 10.0, 10.0))  # never armed
        run_ticks(fs, 200)
        assert fs.position[0] == pytest.approx([0.0, 0.0, 0.0])

    def test_setpoint_latency_delays_response(self):
        fs = FleetState(1, SimConfig(rng_seed=1, setpoint_latency_s=0.5))
        fs.armed[:] = True
        fs.command_setpoint(0, (0.0, 0.0, 5.0))
        run_ticks(fs, 5)  # well under the 0.5s latency
        assert fs.position[0, 2] == pytest.approx(0.0, abs=0.01)
        run_ticks(fs, 500)
        assert fs.position[0, 2] == pytest.approx(5.0, abs=0.15)

    def test_never_exceeds_max_velocity(self):
        fs = FleetState(1, SimConfig(rng_seed=1, max_velocity_ms=2.0))
        fs.armed[:] = True
        fs.command_setpoint(0, (0.0, 0.0, 100.0))  # far target, forces max speed
        for _ in range(300):
            fs.tick(fs.config.dt)
            assert np.linalg.norm(fs.velocity[0]) <= 2.0 + 1e-9


class TestFleetIndependence:
    def test_drones_do_not_cross_talk(self):
        n = 20
        fs = FleetState(n, SimConfig(rng_seed=2, wind_std_ms2=0.0))
        fs.armed[:] = True
        for i in range(n):
            fs.command_setpoint(i, (float(i), 0.0, 2.0))
        run_ticks(fs, 400)
        for i in range(n):
            assert fs.position[i] == pytest.approx([float(i), 0.0, 2.0], abs=0.15)

    @pytest.mark.parametrize("n", [1, 10, 100, 500])
    def test_scales_to_large_fleets_without_error(self, n):
        fs = FleetState(n, SimConfig(rng_seed=3))
        fs.armed[:] = True
        for i in range(n):
            fs.command_setpoint(i, (0.0, 0.0, 2.0))
        run_ticks(fs, 50)
        assert fs.position.shape == (n, 3)


class TestLandingAndRTL:
    def test_land_settles_exactly_at_ground_not_below(self):
        fs = FleetState(1, SimConfig(rng_seed=4))
        fs.armed[:] = True
        fs.command_setpoint(0, (0.0, 0.0, 5.0))
        run_ticks(fs, 500)
        fs.command_land(0)
        run_ticks(fs, 3000)
        assert not fs.armed[0]
        assert fs.position[0, 2] == pytest.approx(0.0, abs=1e-6)
        assert fs.flight_mode[0] == "LANDED"

    def test_ground_is_a_hard_floor_even_mid_descent(self):
        fs = FleetState(1, SimConfig(rng_seed=5))
        fs.armed[:] = True
        fs.command_setpoint(0, (0.0, 0.0, 5.0))
        run_ticks(fs, 500)
        fs.command_land(0)
        for _ in range(3000):
            fs.tick(fs.config.dt)
            assert fs.position[0, 2] >= -1e-9

    def test_rtl_flies_home_then_lands(self):
        fs = FleetState(1, SimConfig(rng_seed=6))
        fs.armed[0] = True
        fs.position[0] = [10.0, 10.0, 3.0]
        fs.command_rtl(0)
        run_ticks(fs, 6000)
        assert not fs.armed[0]
        assert fs.position[0, :2] == pytest.approx([0.0, 0.0], abs=0.2)
        assert fs.position[0, 2] == pytest.approx(0.0, abs=1e-6)


class TestHealthAndArming:
    def test_not_armable_immediately_after_connect(self):
        fs = FleetState(1, SimConfig(rng_seed=7, armable_delay_s=1.0))
        assert not fs.is_armable(0)

    def test_becomes_armable_after_the_settle_delay(self):
        fs = FleetState(1, SimConfig(rng_seed=7, armable_delay_s=0.2))
        run_ticks(fs, 20)  # 20 * dt(1/50) = 0.4s > 0.2s delay
        assert fs.is_armable(0)

    def test_battery_drains_only_while_armed(self):
        fs = FleetState(2, SimConfig(rng_seed=8, battery_drain_per_s=10.0))
        fs.armed[0] = True
        run_ticks(fs, 100)
        assert fs.battery_pct[0] < 100.0
        assert fs.battery_pct[1] == pytest.approx(100.0)

    def test_battery_does_not_go_negative(self):
        fs = FleetState(1, SimConfig(rng_seed=9, battery_drain_per_s=1000.0))
        fs.armed[:] = True
        run_ticks(fs, 100)
        assert fs.battery_pct[0] == pytest.approx(0.0)


class TestRealismToggles:
    def test_dropout_flips_connectivity_when_enabled(self):
        fs = FleetState(50, SimConfig(rng_seed=10, dropout_prob_per_tick=0.5))
        run_ticks(fs, 5)
        assert not np.all(fs.connected)

    def test_no_dropout_by_default(self):
        fs = FleetState(50, SimConfig(rng_seed=10))
        run_ticks(fs, 100)
        assert np.all(fs.connected)

    def test_position_noise_only_affects_readout_not_physics(self):
        fs = FleetState(1, SimConfig(rng_seed=11, position_noise_std_m=5.0))
        fs.armed[:] = True
        fs.command_setpoint(0, (0.0, 0.0, 2.0))
        run_ticks(fs, 200)
        true_pos = fs.position[0].copy()
        readouts = [fs.noisy_position(0) for _ in range(20)]
        assert np.std([r[2] for r in readouts]) > 0.5  # readout is noisy...
        assert fs.position[0] == pytest.approx(true_pos)  # ...but true state wasn't touched

    def test_zero_noise_is_exact(self):
        fs = FleetState(1, SimConfig(rng_seed=12, position_noise_std_m=0.0))
        fs.position[0] = [1.0, 2.0, 3.0]
        assert fs.noisy_position(0) == pytest.approx([1.0, 2.0, 3.0])
