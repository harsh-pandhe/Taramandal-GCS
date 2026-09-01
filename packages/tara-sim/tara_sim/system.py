"""A `System`-shaped object backed by :class:`~tara_sim.fleet_state.FleetState`.

`tara_gcs.drone_manager.DroneManager` calls exactly 19 methods across 4 MAVSDK
submodules on whatever object it stores at ``self.drones[drone_id]``:
``.core.connection_state()``; ``.telemetry.battery()``, ``.flight_mode()``,
``.gps_info()``, ``.position_velocity_ned()``, ``.armed()``, ``.health()``,
``.home()``; ``.action.arm()``, ``.disarm()``, ``.land()``,
``.return_to_launch()``, ``.get_takeoff_altitude()``; ``.offboard.start()``,
``.stop()``, ``.set_position_ned()``, ``.set_position_global()``,
``.set_position_velocity_acceleration_ned()``; ``.param.set_param_int()``,
``.set_param_float()``.

``SimSystem`` implements exactly that surface — nothing more — so
`DroneManager` cannot tell the difference between a real `mavsdk.System` and
one of these. Every monitor loop, the launch sequence, RTL, land, and the
trajectory playback loop run **completely unchanged** against it.

This module does not import `mavsdk`. The position/velocity/acceleration
objects `DroneManager` passes to ``offboard.set_position_ned`` etc. *are* real
`mavsdk.offboard` types (`DroneManager` constructs them either way), but
`SimSystem` only ever reads their attributes — Python's duck typing means it
never needs the class itself.
"""

from __future__ import annotations

import asyncio

from tara_core import geodetic_to_ned, ned_to_geodetic

from .fleet_state import FleetState

#: Home position used to convert this drone's local frame to lat/lon for
#: `.telemetry.home()` and GLOBAL-mode setpoints, when a `SimFleet` isn't given
#: an explicit venue. Bangalore — matches the KML exporter's existing default.
DEFAULT_VENUE_LAT = 12.9716
DEFAULT_VENUE_LON = 77.5946

#: Update rates for the telemetry streams below. Position/connection/armed
#: are read at a rate that keeps a 5Hz dashboard feed responsive; battery/GPS/
#: flight-mode/home change slowly and are streamed more coarsely.
_FAST_INTERVAL_S = 0.05
_SLOW_INTERVAL_S = 0.5


class _Obj:
    """A tiny attribute bag — avoids hand-writing a class per telemetry shape."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


async def _stream(interval_s: float, read):
    """Yield ``read()`` forever at ``interval_s``, matching MAVSDK's subscription style."""
    while True:
        yield read()
        await asyncio.sleep(interval_s)


class _Telemetry:
    def __init__(self, system: "SimSystem"):
        self._sys = system

    async def battery(self):
        fs, i = self._sys._fs, self._sys.drone_id
        async for _ in _stream(_SLOW_INTERVAL_S, lambda: None):
            pct = float(fs.battery_pct[i])
            yield _Obj(remaining_percent=pct, voltage_v=14.0 + 0.028 * pct)

    async def flight_mode(self):
        fs, i = self._sys._fs, self._sys.drone_id
        async for _ in _stream(_SLOW_INTERVAL_S, lambda: None):
            yield fs.flight_mode[i]

    async def gps_info(self):
        fs, i = self._sys._fs, self._sys.drone_id
        async for _ in _stream(_SLOW_INTERVAL_S, lambda: None):
            yield _Obj(num_satellites=int(fs.satellites[i]), fix_type=fs.gps_fix_type[i])

    async def position_velocity_ned(self):
        fs, i = self._sys._fs, self._sys.drone_id
        async for _ in _stream(_FAST_INTERVAL_S, lambda: None):
            north, east, alt = fs.noisy_position(i)
            vnorth, veast, valt = fs.velocity[i]
            yield _Obj(
                position=_Obj(north_m=float(north), east_m=float(east), down_m=float(-alt)),
                velocity=_Obj(north_m_s=float(vnorth), east_m_s=float(veast), down_m_s=float(-valt)),
            )

    async def armed(self):
        fs, i = self._sys._fs, self._sys.drone_id
        async for _ in _stream(_FAST_INTERVAL_S, lambda: None):
            yield bool(fs.armed[i])

    async def health(self):
        """Mirrors the subset of mavsdk Health that DroneManager reads.

        is_home_position_ok is reported alongside is_armable because the GCS
        launch sequence gates arming on both (see DroneManager._launch_one and
        HOME_LOCK_TIMEOUT_S) — a health object carrying only is_armable would
        make the sim backend raise AttributeError on every launch. It tracks
        is_armable rather than being hardcoded True: this adapter's contract is
        to duck-type MAVSDK, and a drone that is not yet ready to arm has not
        yet settled a home position either. The sim has no GNSS convergence to
        model, so the two become true at the same moment here, whereas on real
        SITL home lags armable — which is exactly the gap the GCS-side wait
        exists to close.
        """
        fs, i = self._sys._fs, self._sys.drone_id
        async for _ in _stream(_SLOW_INTERVAL_S, lambda: None):
            armable = fs.is_armable(i)
            yield _Obj(
                is_armable=armable,
                is_home_position_ok=armable,
                is_global_position_ok=armable,
                is_local_position_ok=armable,
            )

    async def home(self):
        sys_ = self._sys
        async for _ in _stream(_SLOW_INTERVAL_S, lambda: None):
            yield _Obj(latitude_deg=sys_.home_lat, longitude_deg=sys_.home_lon, absolute_altitude_m=920.0)


class _Core:
    def __init__(self, system: "SimSystem"):
        self._sys = system

    async def connection_state(self):
        fs, i = self._sys._fs, self._sys.drone_id
        async for _ in _stream(_FAST_INTERVAL_S, lambda: None):
            yield _Obj(is_connected=bool(fs.connected[i]))


class _Action:
    def __init__(self, system: "SimSystem"):
        self._sys = system

    async def arm(self):
        self._sys._fs.command_arm(self._sys.drone_id)

    async def disarm(self):
        self._sys._fs.command_disarm(self._sys.drone_id)

    async def land(self):
        self._sys._fs.command_land(self._sys.drone_id)

    async def return_to_launch(self):
        self._sys._fs.command_rtl(self._sys.drone_id)

    async def get_takeoff_altitude(self):
        # Used only as a round-trip latency probe (drone_manager._measure_latency_loop);
        # an in-process call correctly reports ~0ms for the sim backend.
        return 2.5


class _Offboard:
    def __init__(self, system: "SimSystem"):
        self._sys = system

    async def start(self):
        self._sys._fs.offboard_active[self._sys.drone_id] = True

    async def stop(self):
        self._sys._fs.offboard_active[self._sys.drone_id] = False

    async def set_position_ned(self, position_ned_yaw):
        p = position_ned_yaw
        self._sys._fs.command_setpoint(
            self._sys.drone_id, (p.north_m, p.east_m, -p.down_m), yaw_deg=p.yaw_deg
        )

    async def set_position_global(self, position_global_yaw):
        p = position_global_yaw
        sys_ = self._sys
        x, y = geodetic_to_ned(p.lat_deg, p.lon_deg, sys_.home_lat, sys_.home_lon)
        sys_._fs.command_setpoint(sys_.drone_id, (x, y, p.alt_m), yaw_deg=p.yaw_deg)

    async def set_position_velocity_acceleration_ned(self, position_ned_yaw, velocity_ned_yaw, acceleration_ned):
        p, v, a = position_ned_yaw, velocity_ned_yaw, acceleration_ned
        self._sys._fs.command_setpoint(
            self._sys.drone_id,
            (p.north_m, p.east_m, -p.down_m),
            velocity_ff=(v.north_m_s, v.east_m_s, -v.down_m_s),
            accel_ff=(a.north_m_s2, a.east_m_s2, -a.down_m_s2),
            yaw_deg=p.yaw_deg,
        )


class _Param:
    async def set_param_int(self, name: str, value: int):
        pass  # no real arming-check subsystem exists to bypass in simulation

    async def set_param_float(self, name: str, value: float):
        pass


class SimSystem:
    """Duck-types `mavsdk.System` for one simulated drone.

    Args:
        fleet_state: The shared physics backing every drone in this fleet.
        drone_id: This drone's index into ``fleet_state``'s arrays.
        home_lat: This drone's own launch-pad latitude (already offset from the
            venue home by its position in the fleet grid).
        home_lon: This drone's own launch-pad longitude.
    """

    def __init__(self, fleet_state: FleetState, drone_id: int, home_lat: float, home_lon: float):
        self._fs = fleet_state
        self.drone_id = drone_id
        self.home_lat = home_lat
        self.home_lon = home_lon

        self.core = _Core(self)
        self.telemetry = _Telemetry(self)
        self.action = _Action(self)
        self.offboard = _Offboard(self)
        self.param = _Param()

    async def connect(self, system_address: str = ""):
        self._fs._connected_since[self.drone_id] = self._fs.time_s


class SimFleet:
    """Owns one :class:`FleetState` and hands out :class:`SimSystem` views over it.

    Args:
        n: Number of drones.
        venue_lat: Latitude of the fleet's shared origin (fleet-frame (0, 0)).
        venue_lon: Longitude of the fleet's shared origin.
        config: Physics/realism tuning; defaults if omitted.
        pad_positions: Optional ``[(x, y), ...]`` fleet-frame launch-pad
            offsets, used only to compute each drone's own geodetic home for
            GLOBAL-mode setpoints and `.telemetry.home()`. Defaults to
            ``tara_core.launch_grid(n)`` — the same grid `drone_manager.py`
            uses, so home coordinates agree between the two.
    """

    def __init__(
        self,
        n: int,
        venue_lat: float = DEFAULT_VENUE_LAT,
        venue_lon: float = DEFAULT_VENUE_LON,
        config=None,
        pad_positions: list[tuple[float, float]] | None = None,
    ):
        self.state = FleetState(n, config=config)
        if pad_positions is None:
            from tara_core import launch_grid

            pad_positions = launch_grid(n)
        self._homes = [ned_to_geodetic(x, y, venue_lat, venue_lon) for x, y in pad_positions]
        self._systems: dict[int, SimSystem] = {}

    def system(self, drone_id: int) -> SimSystem:
        if drone_id not in self._systems:
            home_lat, home_lon = self._homes[drone_id]
            self._systems[drone_id] = SimSystem(self.state, drone_id, home_lat, home_lon)
        return self._systems[drone_id]

    def start(self):
        return self.state.start()

    def stop(self):
        self.state.stop()
