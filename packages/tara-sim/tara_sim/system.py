"""A `System`-shaped object backed by :class:`~tara_sim.fleet_state.FleetState`.

`tara_gcs.drone_manager.DroneManager` calls exactly 23 methods across 5 MAVSDK
submodules on whatever object it stores at ``self.drones[drone_id]``:
``.core.connection_state()``; ``.telemetry.battery()``, ``.flight_mode()``,
``.gps_info()``, ``.position_velocity_ned()``, ``.armed()``, ``.health()``,
``.home()``; ``.action.arm()``, ``.disarm()``, ``.land()``,
``.return_to_launch()``, ``.kill()``, ``.get_takeoff_altitude()``; ``.offboard.start()``,
``.stop()``, ``.set_position_ned()``, ``.set_position_global()``,
``.set_position_velocity_acceleration_ned()``; ``.param.set_param_int()``,
``.set_param_float()``; ``.ftp.create_directory()``, ``.upload()``,
``.are_files_identical()``.

The show broadcast trigger has no MAVSDK method — it is one frame addressed to
every drone at once, not a call on any one of them — so it arrives through
:meth:`SimFleet.broadcast` instead, which plays the part of the drones' shared
radio.

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
from dataclasses import dataclass
import logging
import os
import zlib

import numpy as np
from tara_core import (
    ShowCommand,
    decode_mission,
    decode_show_trigger,
    geodetic_to_ned,
    ned_to_geodetic,
)

from .fleet_state import LANDED_ALTITUDE_M, FleetState

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

#: Every simulated home reports this absolute altitude (metres AMSL).
HOME_ABSOLUTE_ALT_M = 920.0


def _battery_voltage(pct):
    """A 4S pack's voltage for a charge percentage (scalar or array)."""
    return 14.0 + 0.028 * pct


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
            yield _Obj(remaining_percent=pct, voltage_v=_battery_voltage(pct))

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

    async def landed_state(self):
        """Mirrors FleetState._step's own ON_GROUND threshold (same altitude
        cutoff, same vertical-speed deadband), applied to the same noisy
        position every other field here reports rather than ground truth -
        consistent with position_velocity_ned, not a separate measurement.
        Only ON_GROUND/IN_AIR are modelled; the sim has no taking-off/landing
        transition states to report (issue #145).
        """
        fs, i = self._sys._fs, self._sys.drone_id
        async for _ in _stream(_FAST_INTERVAL_S, lambda: None):
            _, _, alt = fs.noisy_position(i)
            vz = float(fs.velocity[i, 2])
            yield "ON_GROUND" if alt <= LANDED_ALTITUDE_M and abs(vz) < 0.1 else "IN_AIR"

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
            yield _Obj(latitude_deg=sys_.home_lat, longitude_deg=sys_.home_lon, absolute_altitude_m=HOME_ABSOLUTE_ALT_M)


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

    async def kill(self):
        self._sys._fs.command_kill(self._sys.drone_id)

    async def get_takeoff_altitude(self):
        # Used only as a round-trip latency probe (drone_manager._measure_latency_loop);
        # an in-process call correctly reports ~0ms for the sim backend.
        return 2.5


class _Offboard:
    def __init__(self, system: "SimSystem"):
        self._sys = system

    async def start(self):
        self._sys._fs.command_offboard_start(self._sys.drone_id)

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


class SimFtpError(Exception):
    """Stands in for ``mavsdk.ftp.FtpError`` — raised on a failed transfer."""


class _Ftp:
    """MAVLink FTP against the drone's onboard storage (``FleetState.storage``).

    Transfers need the link: a drone whose ``connected`` flag is down fails
    them, exactly as a real FTP session would time out.
    """

    def __init__(self, system: "SimSystem"):
        self._sys = system

    def _require_link(self, op: str) -> None:
        if not self._sys._fs.connected[self._sys.drone_id]:
            raise SimFtpError(f"{op}: drone {self._sys.drone_id} is not reachable")

    async def create_directory(self, remote_dir: str):
        self._require_link("create_directory")

    async def upload(self, local_file_path: str, remote_dir: str):
        """Mirrors MAVSDK: an async generator yielding progress until done."""
        self._require_link("upload")
        with open(local_file_path, "rb") as f:
            data = f.read()
        remote_path = f"{remote_dir.rstrip('/')}/{os.path.basename(local_file_path)}"
        self._sys._fs.storage[self._sys.drone_id][remote_path] = data
        yield _Obj(bytes_transferred=len(data), total_bytes=len(data))

    async def are_files_identical(self, local_file_path: str, remote_file_path: str) -> bool:
        """Mirrors MAVSDK: the drone reports its copy's CRC-32, compared locally."""
        self._require_link("are_files_identical")
        stored = self._sys._fs.storage[self._sys.drone_id].get(remote_file_path)
        if stored is None:
            raise SimFtpError(f"are_files_identical: {remote_file_path} not found on drone {self._sys.drone_id}")
        with open(local_file_path, "rb") as f:
            local = f.read()
        return zlib.crc32(stored) == zlib.crc32(local)


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
        self.ftp = _Ftp(self)

    async def connect(self, system_address: str = ""):
        self._fs._connected_since[self.drone_id] = self._fs.time_s


@dataclass(frozen=True)
class BulkTelemetry:
    """One snapshot of every drone's telemetry, as arrays indexed by drone id.

    The fleet-wide equivalent of the eight per-drone streams on
    :class:`SimSystem`, taken in one pass. Arrays are copies, so a consumer
    can't write into the physics state by accident. Position is in each
    drone's own local frame, as ``(north_m, east_m, altitude_m)`` with
    altitude positive-up, and carries the same sensor noise as
    ``position_velocity_ned()``.
    """

    time_s: float
    position: np.ndarray  # (n, 3), noisy
    velocity: np.ndarray  # (n, 3)
    connected: np.ndarray  # (n,) bool
    armed: np.ndarray  # (n,) bool
    battery_pct: np.ndarray  # (n,)
    battery_voltage: np.ndarray  # (n,)
    satellites: np.ndarray  # (n,) int
    flight_mode: list[str]
    gps_fix_type: list[str]
    home_lat: np.ndarray  # (n,)
    home_lon: np.ndarray  # (n,)
    home_alt: np.ndarray  # (n,)


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
        self._home_lat = np.array([lat for lat, _ in self._homes])
        self._home_lon = np.array([lon for _, lon in self._homes])
        self._systems: dict[int, SimSystem] = {}

    def get_bulk_telemetry(self) -> BulkTelemetry:
        """Every drone's telemetry in one vectorized read (issue #51).

        This replaces subscribing to eight streams per drone. At 1,000 drones
        those streams are 8,000 always-on asyncio tasks that saturate the
        event loop, while the data they read already sits in a handful of
        shared arrays.
        """
        fs = self.state
        return BulkTelemetry(
            time_s=fs.time_s,
            position=fs.noisy_positions(),
            velocity=fs.velocity.copy(),
            connected=fs.connected.copy(),
            armed=fs.armed.copy(),
            battery_pct=fs.battery_pct.copy(),
            battery_voltage=_battery_voltage(fs.battery_pct),
            satellites=fs.satellites.copy(),
            flight_mode=list(fs.flight_mode),
            gps_fix_type=list(fs.gps_fix_type),
            home_lat=self._home_lat.copy(),
            home_lon=self._home_lon.copy(),
            home_alt=np.full(fs.n, HOME_ABSOLUTE_ALT_M),
        )

    def system(self, drone_id: int) -> SimSystem:
        if drone_id not in self._systems:
            home_lat, home_lon = self._homes[drone_id]
            self._systems[drone_id] = SimSystem(self.state, drone_id, home_lat, home_lon)
        return self._systems[drone_id]

    def broadcast(self, payload: bytes, target_system: int = 0) -> None:
        """Put one show-trigger frame on the fleet's shared radio.

        Every drone whose link is up hears the same frame; a drone whose link
        is down simply misses it. Each drone then does what its firmware would:
        on START, look through its own storage for a mission matching the
        trigger's show id and addressed to itself, and schedule it; on CANCEL,
        stop and hold. No reply comes back — a broadcast has no acks, and
        whether a drone took the trigger shows up in its telemetry
        (flight mode ``SHOW``), not here.
        """
        if target_system != 0:
            raise ValueError("SimFleet.broadcast only models the broadcast address (target_system=0)")
        try:
            trigger = decode_show_trigger(payload)
        except ValueError:
            return
        if not trigger["valid"]:
            return

        fs = self.state
        for i in range(fs.n):
            if not fs.connected[i]:
                continue
            if trigger["command"] == ShowCommand.CANCEL:
                if fs.mission_show_id[i] == trigger["show_id"]:
                    fs.cancel_mission(i)
                continue
            if not self._load_stored_mission(i, trigger["show_id"]):
                continue
            fs.start_mission(i, trigger["start_utc_usec"] / 1_000_000)

    def _load_stored_mission(self, drone_id: int, show_id: bytes) -> bool:
        """Find, validate and load this drone's stored mission for ``show_id``."""
        fs = self.state
        for path, blob in fs.storage[drone_id].items():
            try:
                header, waypoints = decode_mission(blob)
            except ValueError as e:
                logging.warning(f"Sim drone {drone_id}: ignoring unreadable mission {path}: {e}")
                continue
            if header.show_id == show_id and header.drone_id == drone_id:
                fs.load_mission(drone_id, show_id, waypoints)
                return True
        return False

    def start(self):
        return self.state.start()

    def stop(self):
        self.state.stop()
