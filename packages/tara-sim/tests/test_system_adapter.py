"""Exercises `SimSystem`'s duck-typed MAVSDK surface directly.

Each test drives the exact call `tara_gcs.drone_manager` makes, to prove the
adapter is a faithful stand-in without needing `DroneManager` itself (or
`mavsdk`) in this package's dependencies.
"""

import asyncio

import pytest

from tara_core import ShowCommand, compile_mission, encode_show_trigger, ned_to_geodetic
from tara_sim.fleet_state import SimConfig
from tara_sim.system import SimFleet, SimFtpError


class _Pos:
    def __init__(self, north_m, east_m, down_m, yaw_deg=0.0):
        self.north_m, self.east_m, self.down_m, self.yaw_deg = north_m, east_m, down_m, yaw_deg


class _Vel:
    def __init__(self, north_m_s, east_m_s, down_m_s, yaw_deg=0.0):
        self.north_m_s, self.east_m_s, self.down_m_s, self.yaw_deg = north_m_s, east_m_s, down_m_s, yaw_deg


class _Accel:
    def __init__(self, north_m_s2, east_m_s2, down_m_s2):
        self.north_m_s2, self.east_m_s2, self.down_m_s2 = north_m_s2, east_m_s2, down_m_s2


class _GlobalPos:
    def __init__(self, lat_deg, lon_deg, alt_m, yaw_deg=0.0):
        self.lat_deg, self.lon_deg, self.alt_m, self.yaw_deg = lat_deg, lon_deg, alt_m, yaw_deg


async def anext_(gen):
    return await gen.__anext__()


def run(coro):
    return asyncio.run(coro)


class TestConnectAndTelemetry:
    def test_connect_then_armable_after_delay(self):
        async def go():
            fleet = SimFleet(1, config=SimConfig(armable_delay_s=0.05))
            sys_ = fleet.system(0)
            await sys_.connect()
            health = await anext_(sys_.telemetry.health())
            assert not health.is_armable
            for _ in range(10):
                fleet.state.tick(fleet.state.config.dt)
            health = await anext_(sys_.telemetry.health())
            assert health.is_armable

        run(go())

    def test_battery_reads_from_shared_state(self):
        async def go():
            fleet = SimFleet(1)
            sys_ = fleet.system(0)
            fleet.state.battery_pct[0] = 42.0
            battery = await anext_(sys_.telemetry.battery())
            assert battery.remaining_percent == pytest.approx(42.0)
            assert battery.voltage_v > 0

        run(go())

    def test_position_velocity_ned_matches_true_state(self):
        async def go():
            fleet = SimFleet(1, config=SimConfig(position_noise_std_m=0.0))
            sys_ = fleet.system(0)
            fleet.state.position[0] = [1.0, 2.0, 3.0]
            pv = await anext_(sys_.telemetry.position_velocity_ned())
            assert pv.position.north_m == pytest.approx(1.0)
            assert pv.position.east_m == pytest.approx(2.0)
            assert pv.position.down_m == pytest.approx(-3.0)  # altitude sign-flipped to NED down

        run(go())

    def test_armed_stream_reflects_shared_state(self):
        async def go():
            fleet = SimFleet(1)
            sys_ = fleet.system(0)
            assert not await anext_(sys_.telemetry.armed())
            fleet.state.armed[0] = True
            assert await anext_(sys_.telemetry.armed())

        run(go())

    def test_home_matches_the_drone_s_own_launch_pad(self):
        # Drones far apart in the fleet grid so the pad offset isn't small
        # enough for its lat/lon delta to fall inside pytest.approx's default
        # tolerance and read as "the same as the venue origin".
        async def go():
            fleet = SimFleet(200, venue_lat=10.0, venue_lon=20.0)
            sys_ = fleet.system(150)
            home = await anext_(sys_.telemetry.home())
            assert (home.latitude_deg, home.longitude_deg) == pytest.approx(fleet._homes[150])
            assert home.latitude_deg != pytest.approx(10.0) or home.longitude_deg != pytest.approx(20.0)

        run(go())


class TestFlightCommands:
    def test_arm_disarm(self):
        async def go():
            fleet = SimFleet(1)
            sys_ = fleet.system(0)
            await sys_.action.arm()
            assert fleet.state.armed[0]
            await sys_.action.disarm()
            assert not fleet.state.armed[0]

        run(go())

    def test_offboard_start_stop_never_raises(self):
        async def go():
            fleet = SimFleet(1)
            sys_ = fleet.system(0)
            await sys_.offboard.start()
            assert fleet.state.offboard_active[0]
            await sys_.offboard.stop()
            assert not fleet.state.offboard_active[0]

        run(go())

    def test_set_position_ned_reaches_target(self):
        async def go():
            fleet = SimFleet(1, config=SimConfig(rng_seed=1))
            sys_ = fleet.system(0)
            fleet.state.armed[0] = True
            await sys_.offboard.set_position_ned(_Pos(3.0, -1.0, -4.0, 90.0))
            for _ in range(500):
                fleet.state.tick(fleet.state.config.dt)
            assert fleet.state.position[0] == pytest.approx([3.0, -1.0, 4.0], abs=0.2)

        run(go())

    def test_set_position_velocity_acceleration_ned_feeds_all_three(self):
        # A constant nonzero velocity feedforward against a *fixed* target
        # position is a contradiction a PD tracker can't resolve to a clean
        # stop (real trajectory playback keeps advancing the target position
        # every tick alongside the feedforward, which this single call
        # deliberately does not). So this checks plumbing, not convergence:
        # position/velocity/acceleration/yaw all land in FleetState's target
        # arrays exactly as given — fleet_state.py's own tests already cover
        # the PD math in isolation.
        async def go():
            fleet = SimFleet(1, config=SimConfig(rng_seed=2, setpoint_latency_s=0.0))
            sys_ = fleet.system(0)
            await sys_.offboard.set_position_velocity_acceleration_ned(
                _Pos(2.0, 2.0, -2.0, 45.0), _Vel(0.5, 0.1, -0.2, 45.0), _Accel(0.1, 0.2, -0.3)
            )
            fleet.state.tick(fleet.state.config.dt)  # let the 0-latency setpoint land
            assert fleet.state.target_position[0] == pytest.approx([2.0, 2.0, 2.0])
            assert fleet.state.target_velocity_ff[0] == pytest.approx([0.5, 0.1, 0.2])
            assert fleet.state.target_accel_ff[0] == pytest.approx([0.1, 0.2, 0.3])
            assert fleet.state.target_yaw_deg[0] == pytest.approx(45.0)

        run(go())

    def test_set_position_global_round_trips_through_geodetic(self):
        async def go():
            fleet = SimFleet(1, venue_lat=12.9716, venue_lon=77.5946, config=SimConfig(rng_seed=3))
            sys_ = fleet.system(0)
            fleet.state.armed[0] = True
            target_lat, target_lon = ned_to_geodetic(5.0, -3.0, sys_.home_lat, sys_.home_lon)
            await sys_.offboard.set_position_global(_GlobalPos(target_lat, target_lon, 6.0))
            for _ in range(500):
                fleet.state.tick(fleet.state.config.dt)
            assert fleet.state.position[0] == pytest.approx([5.0, -3.0, 6.0], abs=0.3)

        run(go())

    def test_land_command_drives_the_land_state_machine(self):
        async def go():
            fleet = SimFleet(1, config=SimConfig(rng_seed=4))
            sys_ = fleet.system(0)
            fleet.state.armed[0] = True
            fleet.state.position[0] = [0.0, 0.0, 3.0]
            await sys_.action.land()
            for _ in range(3000):
                fleet.state.tick(fleet.state.config.dt)
                if not fleet.state.armed[0]:
                    break
            assert not fleet.state.armed[0]
            assert fleet.state.position[0, 2] == pytest.approx(0.0, abs=1e-6)

        run(go())

    def test_return_to_launch_flies_home_first(self):
        async def go():
            fleet = SimFleet(1, config=SimConfig(rng_seed=5))
            sys_ = fleet.system(0)
            fleet.state.armed[0] = True
            fleet.state.position[0] = [8.0, 8.0, 3.0]
            await sys_.action.return_to_launch()
            for _ in range(6000):
                fleet.state.tick(fleet.state.config.dt)
                if not fleet.state.armed[0]:
                    break
            assert fleet.state.position[0, :2] == pytest.approx([0.0, 0.0], abs=0.3)

        run(go())

    def test_get_takeoff_altitude_is_near_instant(self):
        async def go():
            fleet = SimFleet(1)
            sys_ = fleet.system(0)
            loop = asyncio.get_event_loop()
            start = loop.time()
            await sys_.action.get_takeoff_altitude()
            assert loop.time() - start < 0.01

        run(go())

    def test_param_setters_are_harmless_no_ops(self):
        async def go():
            fleet = SimFleet(1)
            sys_ = fleet.system(0)
            await sys_.param.set_param_int("COM_ARM_MAG_STR", 0)
            await sys_.param.set_param_float("COM_ARM_IMU_ACC", 10.0)

        run(go())


class TestNoMavsdkDependency:
    def test_tara_sim_source_never_imports_mavsdk(self):
        """Static check, not a sys.modules check.

        A runtime `"mavsdk" not in sys.modules` assertion would be meaningless
        here: pytest collects `tara_gcs`'s tests in the same process, and
        `drone_manager.py` imports `mavsdk` at module level, so by the time
        this test runs mavsdk is already loaded regardless of what tara_sim
        does. What actually matters — that tara_sim's own source never
        references it — has to be checked in the source text instead.
        """
        import inspect

        import tara_sim
        import tara_sim.fleet_state
        import tara_sim.system

        for module in (tara_sim, tara_sim.fleet_state, tara_sim.system):
            source = inspect.getsource(module)
            assert "import mavsdk" not in source and "from mavsdk" not in source


class TestFtpAndBroadcast:
    """The staging half (MAVLink FTP into onboard storage) and the start half
    (one broadcast frame) of an onboard show, issue #47."""

    SHOW_ID = b"\x11" * 16
    REMOTE_DIR = "/fs/microsd/tara"
    WPS = [
        {"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
        {"time": 3.0, "x": 1.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
    ]

    def _write(self, tmp_path, drone_id, show_id=None):
        path = tmp_path / str(drone_id) / "show.tmsn"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(compile_mission(self.WPS, drone_id=drone_id, show_id=show_id or self.SHOW_ID))
        return str(path)

    async def _upload(self, sys_, local):
        async for _ in sys_.ftp.upload(local, self.REMOTE_DIR):
            pass

    def test_upload_stores_the_file_and_crc_compare_passes(self, tmp_path):
        async def go():
            fleet = SimFleet(1)
            sys_ = fleet.system(0)
            local = self._write(tmp_path, 0)
            await self._upload(sys_, local)
            remote = f"{self.REMOTE_DIR}/show.tmsn"
            assert remote in fleet.state.storage[0]
            assert await sys_.ftp.are_files_identical(local, remote)

            fleet.state.storage[0][remote] = b"corrupt"
            assert not await sys_.ftp.are_files_identical(local, remote)

        run(go())

    def test_ftp_fails_without_a_link(self, tmp_path):
        async def go():
            fleet = SimFleet(1)
            fleet.state.connected[0] = False
            with pytest.raises(SimFtpError):
                await self._upload(fleet.system(0), self._write(tmp_path, 0))

        run(go())

    def _staged_fleet(self, tmp_path, n=3):
        fleet = SimFleet(n)
        fleet.state.armed[:] = True

        async def go():
            for i in range(n):
                await self._upload(fleet.system(i), self._write(tmp_path, i))

        run(go())
        return fleet

    def test_start_trigger_schedules_every_listening_drone(self, tmp_path):
        fleet = self._staged_fleet(tmp_path)
        fleet.state.connected[2] = False  # misses the frame
        fleet.broadcast(encode_show_trigger(ShowCommand.START, self.SHOW_ID, 1_234_000_000))
        assert list(fleet.state.mission_active) == [True, True, False]
        assert fleet.state.mission_start_utc[0] == pytest.approx(1234.0)

    def test_trigger_for_another_show_is_ignored(self, tmp_path):
        fleet = self._staged_fleet(tmp_path)
        fleet.broadcast(encode_show_trigger(ShowCommand.START, b"\x22" * 16, 0))
        assert not fleet.state.mission_active.any()

    def test_mission_addressed_to_another_drone_is_not_flown(self, tmp_path):
        fleet = SimFleet(2)
        fleet.state.armed[:] = True
        # Drone 1 is handed drone 0's file.
        run(self._upload(fleet.system(1), self._write(tmp_path, 0)))
        fleet.broadcast(encode_show_trigger(ShowCommand.START, self.SHOW_ID, 0))
        assert not fleet.state.mission_active[1]

    def test_corrupted_trigger_is_ignored(self, tmp_path):
        fleet = self._staged_fleet(tmp_path)
        frame = bytearray(encode_show_trigger(ShowCommand.START, self.SHOW_ID, 0))
        frame[-1] ^= 0xFF
        fleet.broadcast(bytes(frame))
        assert not fleet.state.mission_active.any()

    def test_corrupted_stored_mission_is_not_flown(self, tmp_path):
        fleet = self._staged_fleet(tmp_path, n=1)
        remote = f"{self.REMOTE_DIR}/show.tmsn"
        blob = bytearray(fleet.state.storage[0][remote])
        blob[-3] ^= 0x01
        fleet.state.storage[0][remote] = bytes(blob)
        fleet.broadcast(encode_show_trigger(ShowCommand.START, self.SHOW_ID, 0))
        assert not fleet.state.mission_active[0]

    def test_cancel_trigger_stops_the_show(self, tmp_path):
        fleet = self._staged_fleet(tmp_path)
        fleet.broadcast(encode_show_trigger(ShowCommand.START, self.SHOW_ID, 0))
        fleet.broadcast(encode_show_trigger(ShowCommand.CANCEL, self.SHOW_ID))
        assert not fleet.state.mission_active.any()
