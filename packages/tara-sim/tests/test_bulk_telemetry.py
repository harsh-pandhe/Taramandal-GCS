"""`SimFleet.get_bulk_telemetry` and the deadline-scheduled tick loop (issue #51)."""

import asyncio
import time

import numpy as np
import pytest

from tara_sim.fleet_state import MAX_TICK_BACKLOG_S, FleetState, SimConfig
from tara_sim.system import HOME_ABSOLUTE_ALT_M, SimFleet


async def anext_(gen):
    return await gen.__anext__()


class TestBulkTelemetry:
    def test_matches_every_per_drone_stream(self):
        """The bulk snapshot reports what each drone's own streams would."""

        async def go():
            fleet = SimFleet(6, config=SimConfig(position_noise_std_m=0.0, rng_seed=3))
            fs = fleet.state
            fs.position[:] = np.arange(18, dtype=float).reshape(6, 3)
            fs.velocity[:] = -np.arange(18, dtype=float).reshape(6, 3)
            fs.armed[[1, 4]] = True
            fs.connected[2] = False
            fs.battery_pct[:] = [100, 90, 80, 70, 60, 50]
            fs.satellites[3] = 4
            fs.flight_mode[4] = "OFFBOARD"
            fs.gps_fix_type[5] = "NO_FIX"

            snap = fleet.get_bulk_telemetry()

            for i in range(6):
                sys_ = fleet.system(i)
                pv = await anext_(sys_.telemetry.position_velocity_ned())
                assert snap.position[i].tolist() == [pv.position.north_m, pv.position.east_m, -pv.position.down_m]
                assert snap.velocity[i].tolist() == [pv.velocity.north_m_s, pv.velocity.east_m_s, -pv.velocity.down_m_s]
                assert snap.armed[i] == await anext_(sys_.telemetry.armed())
                assert snap.connected[i] == (await anext_(sys_.core.connection_state())).is_connected
                battery = await anext_(sys_.telemetry.battery())
                assert snap.battery_pct[i] == battery.remaining_percent
                assert snap.battery_voltage[i] == pytest.approx(battery.voltage_v)
                assert snap.flight_mode[i] == await anext_(sys_.telemetry.flight_mode())
                gps = await anext_(sys_.telemetry.gps_info())
                assert (snap.satellites[i], snap.gps_fix_type[i]) == (gps.num_satellites, gps.fix_type)
                home = await anext_(sys_.telemetry.home())
                assert (snap.home_lat[i], snap.home_lon[i]) == (home.latitude_deg, home.longitude_deg)
                assert snap.home_alt[i] == home.absolute_altitude_m == HOME_ABSOLUTE_ALT_M

        asyncio.run(go())

    def test_position_carries_sensor_noise(self):
        fleet = SimFleet(200, config=SimConfig(position_noise_std_m=0.5, rng_seed=1))
        err = fleet.get_bulk_telemetry().position - fleet.state.position
        assert err.shape == (200, 3)
        assert 0.4 < err.std() < 0.6

    def test_snapshot_is_a_copy(self):
        fleet = SimFleet(3)
        snap = fleet.get_bulk_telemetry()
        snap.armed[:] = True
        snap.position[:] = 99.0
        snap.flight_mode[0] = "HACKED"
        assert not fleet.state.armed.any()
        assert not (fleet.state.position == 99.0).any()
        assert fleet.state.flight_mode[0] == "MANUAL"

    def test_one_read_scales_to_a_full_fleet(self):
        fleet = SimFleet(1000)
        snap = fleet.get_bulk_telemetry()
        assert snap.position.shape == (1000, 3)
        assert len(snap.flight_mode) == len(snap.home_lat) == 1000


class TestDeadlineScheduledTick:
    def _run_for(self, fs: FleetState, seconds: float) -> float:
        """Run the real tick loop for ``seconds`` of wall-clock; return simulated seconds advanced."""

        async def go():
            fs.start()
            t0, sim0 = time.perf_counter(), fs.time_s
            await asyncio.sleep(seconds)
            fs.stop()
            return fs.time_s - sim0, time.perf_counter() - t0

        sim_s, wall_s = asyncio.run(go())
        return sim_s / wall_s

    def test_keeps_real_time_even_when_each_tick_costs_a_third_of_dt(self):
        """Sleeping a full dt after each tick would run at ~0.75x here."""
        fs = FleetState(10, SimConfig(tick_hz=50.0))
        work_s = fs.config.dt / 3
        original = fs.tick

        def costly_tick(dt):
            end = time.perf_counter() + work_s
            while time.perf_counter() < end:
                pass
            original(dt)

        fs.tick = costly_tick
        assert self._run_for(fs, 1.0) >= 0.97

    def test_a_long_stall_is_dropped_not_replayed_as_a_burst(self):
        """After a 0.5s stall the loop catches up at most MAX_TICK_BACKLOG_S, then re-anchors."""
        fs = FleetState(1, SimConfig(tick_hz=50.0))
        dt = fs.config.dt
        stamps = []
        original = fs.tick

        def stalling_tick(d):
            stamps.append(time.perf_counter())
            if len(stamps) == 5:
                time.sleep(0.5)
            original(d)

        fs.tick = stalling_tick
        self._run_for(fs, 1.0)

        after_stall = np.diff(stamps[5:])
        back_to_back = int(np.sum(after_stall < dt / 4))
        assert back_to_back <= round(MAX_TICK_BACKLOG_S / dt) + 1
