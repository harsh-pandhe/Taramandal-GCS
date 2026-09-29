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


class _Clock:
    """A pinned UTC clock that advances with the physics, so the onboard
    mission executor can be driven deterministically."""

    def __init__(self, fs: FleetState, t0: float = 1_000.0):
        self.fs, self.t0 = fs, t0

    def __call__(self) -> float:
        return self.t0 + self.fs.time_s


def _mission_fleet(n: int = 1) -> tuple[FleetState, _Clock]:
    fs = FleetState(n, SimConfig(rng_seed=13, wind_std_ms2=0.0, position_noise_std_m=0.0))
    clock = _Clock(fs)
    fs.utc_clock = clock
    fs.armed[:] = True
    return fs, clock


# Drone-local NED: climb to 4m, then move 3m north.
_MISSION = [
    {"time": 0.0, "x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0},
    {"time": 2.0, "x": 0.0, "y": 0.0, "z": -4.0, "yaw": 0.0},
    {"time": 4.0, "x": 3.0, "y": 0.0, "z": -4.0, "yaw": 90.0},
]


class TestOnboardMission:
    def test_flies_the_stored_mission_and_holds_its_last_waypoint(self):
        fs, clock = _mission_fleet()
        fs.load_mission(0, b"s" * 16, _MISSION)
        assert fs.start_mission(0, clock())
        run_ticks(fs, int(3.0 * fs.config.tick_hz))
        assert fs.flight_mode[0] == "SHOW"
        run_ticks(fs, int(3.0 * fs.config.tick_hz))
        assert fs.mission_complete[0] and not fs.mission_active[0]
        assert fs.position[0] == pytest.approx([3.0, 0.0, 4.0], abs=0.15)
        assert fs.flight_mode[0] == "HOLD"

    def test_holds_until_the_start_instant(self):
        fs, clock = _mission_fleet()
        fs.load_mission(0, b"s" * 16, _MISSION)
        fs.start_mission(0, clock() + 2.0)
        run_ticks(fs, int(1.5 * fs.config.tick_hz))
        assert fs.position[0] == pytest.approx([0.0, 0.0, 0.0], abs=0.01)
        run_ticks(fs, int(2.5 * fs.config.tick_hz))
        assert fs.position[0, 2] > 2.0

    def test_needs_no_setpoints_from_the_gcs(self):
        fs, clock = _mission_fleet()
        fs.load_mission(0, b"s" * 16, _MISSION)
        fs.start_mission(0, clock())
        run_ticks(fs, int(5.0 * fs.config.tick_hz))
        assert fs.setpoints_received[0] == 0
        assert fs.mission_complete[0]

    def test_refuses_to_start_disarmed_or_with_nothing_loaded(self):
        fs, clock = _mission_fleet(2)
        fs.armed[1] = False
        fs.load_mission(1, b"s" * 16, _MISSION)
        assert not fs.start_mission(0, clock())  # armed, nothing loaded
        assert not fs.start_mission(1, clock())  # loaded, not armed

    def test_cancel_holds_where_it_is(self):
        fs, clock = _mission_fleet()
        fs.load_mission(0, b"s" * 16, _MISSION)
        fs.start_mission(0, clock())
        run_ticks(fs, int(1.5 * fs.config.tick_hz))
        fs.cancel_mission(0)
        held = fs.position[0].copy()
        run_ticks(fs, int(3.0 * fs.config.tick_hz))
        assert not fs.mission_complete[0]
        assert fs.position[0] == pytest.approx(held, abs=0.3)

    @pytest.mark.parametrize("command", ["command_land", "command_rtl", "command_disarm"])
    def test_land_rtl_and_disarm_override_the_mission(self, command):
        fs, clock = _mission_fleet()
        fs.load_mission(0, b"s" * 16, _MISSION)
        fs.start_mission(0, clock())
        run_ticks(fs, 10)
        getattr(fs, command)(0)
        assert not fs.mission_active[0]

    def test_offboard_takes_over_from_the_mission(self):
        fs, clock = _mission_fleet()
        fs.load_mission(0, b"s" * 16, _MISSION)
        fs.start_mission(0, clock())
        fs.command_offboard_start(0)
        assert not fs.mission_active[0]


class TestOffboardLossFailsafe:
    def _offboard_hover(self) -> FleetState:
        fs = FleetState(1, SimConfig(rng_seed=14, wind_std_ms2=0.0))
        fs.armed[:] = True
        fs.command_offboard_start(0)
        fs.command_setpoint(0, (0.0, 0.0, 3.0))
        run_ticks(fs, 100)
        return fs

    def test_quiet_gcs_with_link_up_does_not_trip_it(self):
        # MAVSDK re-sends the last setpoint while offboard is engaged.
        fs = self._offboard_hover()
        run_ticks(fs, int(5.0 * fs.config.tick_hz))
        assert not fs.offboard_failsafe[0]
        assert fs.flight_mode[0] == "OFFBOARD"

    def test_link_loss_in_offboard_fails_safe_to_land(self):
        fs = self._offboard_hover()
        fs.connected[0] = False
        run_ticks(fs, int(0.5 * fs.config.tick_hz))
        assert not fs.offboard_failsafe[0]  # still inside COM_OF_LOSS_T
        run_ticks(fs, int(1.0 * fs.config.tick_hz))
        assert fs.offboard_failsafe[0]
        assert fs.flight_mode[0] == "LAND"

    def test_link_loss_outside_offboard_is_harmless(self):
        fs = self._offboard_hover()
        fs.offboard_active[0] = False
        fs.connected[0] = False
        run_ticks(fs, int(3.0 * fs.config.tick_hz))
        assert not fs.offboard_failsafe[0]
        assert fs.flight_mode[0] == "HOLD"
