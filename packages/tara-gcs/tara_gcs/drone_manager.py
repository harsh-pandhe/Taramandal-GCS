import asyncio
import csv
import math
import logging
import time
from scipy.spatial import cKDTree
from tara_core import DEFAULT_LIMITS, MAX_FLEET_SIZE, altitude_layers, interpolate_waypoint, launch_grid
from mavsdk import System
from mavsdk.offboard import PositionNedYaw, PositionGlobalYaw, OffboardError, VelocityNedYaw, AccelerationNed
from tara_gcs.state_store import DEFAULT_STATE_PATH, discard_show_state, load_show_state, save_show_state

# Fleet-wide safety limits and launch geometry come from tara_core so that this
# runtime monitor, the upload gate and the Studio's design-time validator can no
# longer disagree. Previously the altitudes here rose in 0.7m steps — below both
# the 0.8m the Studio checked and the 1.5m this module aborts on, meaning a
# fleet was in violation while still hovering over its own pads.
#
# MAX_FLEET_SIZE now lives in tara_core (not a local constant here) so the
# Studio generator's default spawn positions are built from the exact same
# fixed-size grid — the two previously disagreed past 32 drones, since this
# module always used tara_core.launch_grid(MAX_FLEET_SIZE) while the Studio
# generator used its own 1D line, and the two only coincided by accident for
# fleets that fit in the grid's first row.
DEFAULT_ALTITUDES = altitude_layers(MAX_FLEET_SIZE)

# Each drone's PX4/SITL local NED origin is set at its own spawn/launch pad, so
# per-drone "local_x/y/z" telemetry is NOT directly comparable across drones.
# sim/px4/launch_fleet.sh spawns pads on the same grid tara_core.launch_grid
# defines, and the Studio's generator bakes that grid into trajectory
# coordinates (a shared "fleet frame"). This offset converts between the two.
LAUNCH_PAD_SPACING_Y_M = DEFAULT_LIMITS.design_separation_m


#: Pads for the largest fleet we support, computed once. Indexing into a single
#: fixed grid matters: deriving it per-drone from the current fleet size would
#: move a drone's pad every time the fleet grew, because the grid's width is
#: ceil(sqrt(n)).
_PAD_GRID = launch_grid(MAX_FLEET_SIZE)


def _pad_offset(drone_id: int) -> tuple[float, float]:
    """Returns (x, y) offset of a drone's own launch pad within the shared fleet frame."""
    if not 0 <= drone_id < MAX_FLEET_SIZE:
        raise ValueError(
            f"drone_id {drone_id} outside supported fleet size 0..{MAX_FLEET_SIZE - 1}"
        )
    return _PAD_GRID[drone_id]

class DroneManager:
    def __init__(
        self,
        ports: list[int] = [14540, 14541, 14542],
        backend: str = "px4",
        state_path: str = DEFAULT_STATE_PATH,
    ):
        """
        Args:
            ports: UDP MAVLink ports, one per drone (backend="px4" only —
                length still determines fleet size when backend="sim", but the
                port *values* are unused since there is no real network layer).
            backend: "px4" (default) connects to real MAVSDK/PX4 SITL exactly
                as before. "sim" hands every drone a
                tara_sim.system.SimSystem view over one shared, vectorized
                tara_sim.FleetState instead — a single background tick task for
                the whole fleet rather than a mavsdk_server process and 8
                asyncio tasks per drone, which is what makes fleets of 100+
                practical on one machine. SimSystem duck-types the exact
                mavsdk.System surface this class calls, so every monitor loop,
                the launch sequence, RTL, land, and the trajectory playback
                loop below run completely unchanged on either backend.
            state_path: where the active show (trajectory + playback position)
                is persisted so a GCS restart can offer to resume it (issue #4).
                Overridable so tests don't collide on/pollute the real file.
        """
        if backend not in ("px4", "sim"):
            raise ValueError(f"backend must be 'px4' or 'sim', got {backend!r}")
        self.ports = ports
        self.backend = backend
        self._sim_fleet = None  # tara_sim.SimFleet, created in connect_all() when backend == "sim"
        self.drones = {}  # drone_id -> System (real or SimSystem)
        self.telemetry = {}  # drone_id -> status dict
        self._tasks = []
        self.trajectory_task = None
        self.is_running_trajectory = False
        self.geofence_radius = DEFAULT_LIMITS.geofence_radius_m  # horizontal distance from home (0,0)
        self.geofence_breaches = {}  # drone_id -> bool, tracks active geofence breaches
        self.proximity_breaches = {}  # tracks active proximity breaches per drone pair: (id1, id2) -> bool
        self.active_trajectory = None  # active trajectory stored for verification and start

        # Low-rate telemetry recording for post-flight CSV export (issue #8).
        # Independent of the 10Hz control/safety loops above.
        self.telemetry_log = []  # list of per-sample dicts, appended during a show
        self._telemetry_recording_task = None
        self.is_recording_telemetry = False

        # Show-state persistence (issue #4): periodically snapshot the active
        # trajectory + playback position so a crashed/restarted GCS can offer
        # to resume instead of starting blind.
        self.state_path = state_path
        self.show_mode = "LOCAL"
        self.show_started_at = None  # time.time() when the current trajectory began playing
        self.pending_resume_state = None  # loaded-but-not-yet-applied saved state, set by load_pending_resume_state()
        self._state_snapshot_task = None

    async def connect_all(self) -> None:
        """Connects to all drones concurrently."""
        logging.info(f"Connecting to {len(self.ports)} drones (backend={self.backend}) on ports: {self.ports}")

        if self.backend == "sim":
            from tara_sim import SimFleet

            self._sim_fleet = SimFleet(len(self.ports))
            self._tasks.append(self._sim_fleet.start())

        connect_tasks = []
        for i, port in enumerate(self.ports):
            connect_tasks.append(self._connect_drone(i, port))

        # Use return_exceptions=True so that one failing drone doesn't break GCS startup
        results = await asyncio.gather(*connect_tasks, return_exceptions=True)
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                logging.error(f"Failed to connect Drone {i} on port {self.ports[i]}: {res}")

        # Start proximity safety monitor loop
        self._tasks.append(asyncio.create_task(self._proximity_monitor_loop()))
        # Start geofence enforcement loop
        self._tasks.append(asyncio.create_task(self._geofence_monitor_loop()))

    async def _connect_drone(self, drone_id: int, port: int):
        try:
            if self.backend == "sim":
                drone = self._sim_fleet.system(drone_id)
                await drone.connect()
            else:
                # Use unique gRPC port for each drone to avoid conflicts
                drone = System(port=50051 + drone_id)
                await drone.connect(system_address=f"udpin://127.0.0.1:{port}")

            self.drones[drone_id] = drone
            self.telemetry[drone_id] = {
                "id": drone_id,
                "port": port,
                "connected": False,
                "armed": False,
                "flight_mode": "UNKNOWN",
                "battery_percent": 0.0,
                "battery_voltage": 0.0,
                "gps_lock": False,
                "satellites": 0,
                "gps_fix_type": "NO_GPS",
                "comm_latency_ms": 0.0,
                "home_lat": 0.0,
                "home_lon": 0.0,
                "home_alt": 0.0,
                "local_x": 0.0,
                "local_y": 0.0,
                "local_z": 0.0,
                "fleet_x": 0.0,
                "fleet_y": 0.0,
                "fleet_z": 0.0,
                "heading": 0.0
            }

            # Start telemetry loop for this drone
            self._tasks.append(asyncio.create_task(self._monitor_connection(drone_id)))
            self._tasks.append(asyncio.create_task(self._monitor_battery(drone_id)))
            self._tasks.append(asyncio.create_task(self._monitor_flight_mode(drone_id)))
            self._tasks.append(asyncio.create_task(self._monitor_gps(drone_id)))
            self._tasks.append(asyncio.create_task(self._monitor_position(drone_id)))
            self._tasks.append(asyncio.create_task(self._monitor_armed_state(drone_id)))
            self._tasks.append(asyncio.create_task(self._measure_latency_loop(drone_id)))
            self._tasks.append(asyncio.create_task(self._monitor_home(drone_id)))
        except Exception as e:
            logging.error(f"Error during _connect_drone initialization for Drone {drone_id}: {e}")
            raise e

    async def shutdown(self):
        """Cleans up all background monitoring tasks and disarms drones safely."""
        logging.info("Shutting down DroneManager...")

        # Stop telemetry recording if running
        self.stop_telemetry_recording()

        # If a show is active, persist one last snapshot before tearing down so
        # a restart can offer to resume from here (issue #4) — deliberately not
        # calling stop_trajectory()/discard_show_state() below, since shutdown
        # is not "the show ended," it's "GCS is going away mid-show."
        self.stop_state_snapshotting()
        self._write_state_snapshot()

        # Stop trajectory playback if running
        if self.trajectory_task:
            self.trajectory_task.cancel()
            try:
                await self.trajectory_task
            except asyncio.CancelledError:
                pass
            self.trajectory_task = None
            self.is_running_trajectory = False

        # Disarm active armed drones
        disarm_tasks = []
        for drone_id, drone in self.drones.items():
            if self.telemetry.get(drone_id, {}).get("armed", False):
                async def safe_disarm(d_id, d_obj):
                    try:
                        logging.info(f"Disarming Drone {d_id} during GCS shutdown...")
                        await d_obj.action.disarm()
                    except Exception as e:
                        logging.error(f"Failed to disarm Drone {d_id} on shutdown: {e}")
                disarm_tasks.append(safe_disarm(drone_id, drone))
        if disarm_tasks:
            await asyncio.gather(*disarm_tasks, return_exceptions=True)

        # Cancel telemetry & safety monitoring loops
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

        self.drones.clear()

    # --- Telemetry loops ---

    async def _monitor_connection(self, drone_id):
        try:
            drone = self.drones[drone_id]
            async for state in drone.core.connection_state():
                self.telemetry[drone_id]["connected"] = state.is_connected
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error(f"Error in _monitor_connection for Drone {drone_id}: {e}")

    async def _monitor_battery(self, drone_id):
        try:
            drone = self.drones[drone_id]
            async for battery in drone.telemetry.battery():
                # MAVSDK's remaining_percent is already in the 0-100 range, not a 0-1 fraction.
                self.telemetry[drone_id]["battery_percent"] = round(battery.remaining_percent, 1)
                self.telemetry[drone_id]["battery_voltage"] = round(battery.voltage_v, 2)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error(f"Error in _monitor_battery for Drone {drone_id}: {e}")

    async def _monitor_flight_mode(self, drone_id):
        try:
            drone = self.drones[drone_id]
            async for fm in drone.telemetry.flight_mode():
                self.telemetry[drone_id]["flight_mode"] = str(fm)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error(f"Error in _monitor_flight_mode for Drone {drone_id}: {e}")

    async def _monitor_gps(self, drone_id):
        try:
            drone = self.drones[drone_id]
            async for gps in drone.telemetry.gps_info():
                self.telemetry[drone_id]["satellites"] = gps.num_satellites
                # Arbitrary check for GPS lock
                self.telemetry[drone_id]["gps_lock"] = gps.num_satellites >= 6
                self.telemetry[drone_id]["gps_fix_type"] = str(gps.fix_type)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error(f"Error in _monitor_gps for Drone {drone_id}: {e}")

    async def _monitor_position(self, drone_id):
        try:
            drone = self.drones[drone_id]
            pad_x_off, pad_y_off = _pad_offset(drone_id)
            async for pos in drone.telemetry.position_velocity_ned():
                local_x = round(pos.position.north_m, 2)
                local_y = round(pos.position.east_m, 2)
                # NED: convert Down (negative) back to Altitude (positive) for frontend display
                local_z = round(-pos.position.down_m, 2)
                self.telemetry[drone_id]["local_x"] = local_x
                self.telemetry[drone_id]["local_y"] = local_y
                self.telemetry[drone_id]["local_z"] = local_z
                # Fleet frame: per-drone local position shifted into the shared
                # trajectory/fleet coordinate space (see LAUNCH_PAD_SPACING_Y_M above).
                self.telemetry[drone_id]["fleet_x"] = round(local_x + pad_x_off, 2)
                self.telemetry[drone_id]["fleet_y"] = round(local_y + pad_y_off, 2)
                self.telemetry[drone_id]["fleet_z"] = local_z
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error(f"Error in _monitor_position for Drone {drone_id}: {e}")

    async def _monitor_armed_state(self, drone_id):
        try:
            drone = self.drones[drone_id]
            async for armed in drone.telemetry.armed():
                prev_armed = self.telemetry[drone_id]["armed"]
                self.telemetry[drone_id]["armed"] = armed
                # Reset geofence breach tracking when drone disarms
                if prev_armed and not armed:
                    if drone_id in self.geofence_breaches:
                        self.geofence_breaches[drone_id] = False
                        logging.info(f"Drone {drone_id} disarmed — geofence breach state reset.")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error(f"Error in _monitor_armed_state for Drone {drone_id}: {e}")

    async def _measure_latency_loop(self, drone_id):
        try:
            drone = self.drones[drone_id]
            while True:
                if not self.telemetry[drone_id]["connected"]:
                    self.telemetry[drone_id]["comm_latency_ms"] = 0.0
                    await asyncio.sleep(1.0)
                    continue
                try:
                    start_time = asyncio.get_event_loop().time()
                    await drone.action.get_takeoff_altitude()
                    end_time = asyncio.get_event_loop().time()
                    latency_ms = round((end_time - start_time) * 1000.0, 1)
                    self.telemetry[drone_id]["comm_latency_ms"] = latency_ms
                except Exception:
                    self.telemetry[drone_id]["comm_latency_ms"] = -1.0
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error(f"Error in _measure_latency_loop for Drone {drone_id}: {e}")

    async def _monitor_home(self, drone_id):
        try:
            drone = self.drones[drone_id]
            async for home in drone.telemetry.home():
                self.telemetry[drone_id]["home_lat"] = home.latitude_deg
                self.telemetry[drone_id]["home_lon"] = home.longitude_deg
                self.telemetry[drone_id]["home_alt"] = home.absolute_altitude_m
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error(f"Error in _monitor_home for Drone {drone_id}: {e}")

    # --- Flight commands ---

    # Max drones armed/climbed concurrently in one launch batch. Keeps gRPC/CPU
    # load bounded as fleet size grows, instead of one drone at a time.
    LAUNCH_BATCH_SIZE = 5

    async def launch_sequence(self):
        """
        Arm, switch to offboard, and take off in bounded-concurrency batches,
        with each drone in a batch staggered by 1s (preserves the original
        spatial/temporal launch stagger without serializing the whole fleet).

        Idempotent: already-armed drones are skipped, so re-calling this to
        pick up stragglers (e.g. drones that missed the armable window) does
        not re-command drones that are already flying back to (0,0,0).
        """
        logging.info("Launching fleet (batched, concurrent)...")
        drone_ids = list(self.drones.keys())
        for batch_start in range(0, len(drone_ids), self.LAUNCH_BATCH_SIZE):
            batch = drone_ids[batch_start:batch_start + self.LAUNCH_BATCH_SIZE]
            await asyncio.gather(*(self._launch_one(drone_id, stagger_index=i) for i, drone_id in enumerate(batch)))

    async def _launch_one(self, drone_id: int, stagger_index: int = 0):
        """Arms, engages offboard, and climbs a single drone. Safe to re-call on an already-armed drone (no-op)."""
        if self.telemetry[drone_id]["armed"]:
            logging.info(f"Drone {drone_id} already armed — skipping (idempotent launch).")
            return

        drone = self.drones[drone_id]
        if not self.telemetry[drone_id]["connected"]:
            logging.warning(f"Skipping Drone {drone_id}: Disconnected")
            return

        # Preserve the original per-drone launch stagger within a batch.
        await asyncio.sleep(stagger_index * 1.0)

        try:
            # Bypass magnetic and IMU calibration checks to prevent arming failure in simulation
            try:
                await drone.param.set_param_int("COM_ARM_MAG_STR", 0)
                await drone.param.set_param_int("EKF2_MAG_CHECK", 0)
                await drone.param.set_param_float("COM_ARM_IMU_ACC", 10.0)
                await drone.param.set_param_float("COM_ARM_IMU_GYR", 10.0)
            except Exception as e:
                logging.warning(f"Failed to set calibration bypass parameters for Drone {drone_id}: {e}")

            # Wait for drone to become armable (bounded, so one stuck drone
            # can't block the rest of the fleet — and callers can safely
            # re-invoke launch_sequence() to retry just the stragglers).
            logging.info(f"Drone {drone_id} waiting to become armable...")
            async def _wait_armable():
                async for health in drone.telemetry.health():
                    if health.is_armable:
                        logging.info(f"Drone {drone_id} is now armable.")
                        break
            try:
                await asyncio.wait_for(_wait_armable(), timeout=15.0)
            except asyncio.TimeoutError:
                logging.error(f"Drone {drone_id} did not become armable within 15s — skipping (retry later via /api/launch).")
                return

            # Arm
            try:
                await drone.action.arm()
                logging.info(f"Drone {drone_id} ARMED.")
            except Exception as e:
                logging.error(f"Arming Drone {drone_id} failed: {e}")
                return

            # Set initial position NED (0,0,0) before offboard
            try:
                await drone.offboard.set_position_ned(PositionNedYaw(0.0, 0.0, 0.0, 0.0))
            except Exception as e:
                logging.error(f"Setting initial position NED failed for Drone {drone_id}: {e}")
                return

            # Start offboard mode
            try:
                await drone.offboard.start()
                logging.info(f"Drone {drone_id} entered OFFBOARD mode.")
            except OffboardError as e:
                logging.error(f"Offboard start failed for Drone {drone_id}: {e._result.result}")
                await drone.action.disarm()
                return
            except Exception as e:
                logging.error(f"Offboard start failed for Drone {drone_id}: {e}")
                await drone.action.disarm()
                return

            # Ascend to target altitude (spatial separation)
            alt = DEFAULT_ALTITUDES[drone_id] if drone_id < len(DEFAULT_ALTITUDES) else 3.0
            logging.info(f"Drone {drone_id} climbing to target hover altitude: {alt}m...")
            try:
                await drone.offboard.set_position_ned(PositionNedYaw(0.0, 0.0, -alt, 0.0))
            except Exception as e:
                logging.error(f"Climbing to target altitude failed for Drone {drone_id}: {e}")
                return

        except Exception as e:
            logging.error(f"Unexpected error launching Drone {drone_id}: {e}")

    async def trigger_rtl(self):
        """Trigger RTL for all connected drones."""
        print("FAILSAFE: Triggering Return to Launch (RTL) for all drones...")
        self.stop_trajectory()
        
        rtl_tasks = []
        for drone_id, drone in self.drones.items():
            async def stop_and_rtl(d_id, d_obj):
                try:
                    await d_obj.offboard.stop()
                except Exception as e:
                    pass  # Ignore if not in offboard
                try:
                    await d_obj.action.return_to_launch()
                    print(f"Drone {d_id} commanded to RTL.")
                except Exception as e:
                    print(f"Failed to command RTL on Drone {d_id}: {e}")
                    
            rtl_tasks.append(stop_and_rtl(drone_id, drone))
            
        await asyncio.gather(*rtl_tasks)

    async def trigger_land(self):
        """Trigger Land for all connected drones."""
        print("Commanding all drones to Land...")
        self.stop_trajectory()
        
        land_tasks = []
        for drone_id, drone in self.drones.items():
            async def stop_and_land(d_id, d_obj):
                try:
                    await d_obj.offboard.stop()
                except Exception as e:
                    pass
                try:
                    await d_obj.action.land()
                    print(f"Drone {d_id} commanded to Land.")
                except Exception as e:
                    print(f"Failed to command Landing on Drone {d_id}: {e}")
                    
            land_tasks.append(stop_and_land(drone_id, drone))
            
        await asyncio.gather(*land_tasks)

    # --- Trajectory Playback ---

    def start_trajectory(self, trajectory_data: dict, mode: str = "LOCAL", trigger_epoch_ms: float = 0.0):
        """Start playing back waypoints in a background asyncio task."""
        self.stop_trajectory()
        self.active_trajectory = trajectory_data
        self.show_mode = mode
        self.show_started_at = time.time()
        # Remap trajectory slots to currently healthy drones before playback
        remapped = self.remap_trajectory_to_healthy_drones(trajectory_data)
        self.is_running_trajectory = True
        self.start_telemetry_recording()
        self.start_state_snapshotting()
        self.trajectory_task = asyncio.create_task(self._run_trajectory_loop(remapped, mode, trigger_epoch_ms))

    def stop_trajectory(self):
        """Stop current trajectory playback."""
        self.is_running_trajectory = False
        self.stop_telemetry_recording()
        self.stop_state_snapshotting()
        # A deliberate stop ends the show; there is nothing left to offer resuming.
        discard_show_state(self.state_path)
        if self.trajectory_task:
            self.trajectory_task.cancel()
            self.trajectory_task = None
            print("Trajectory playback stopped.")

    # --- Telemetry Recording & CSV Export (issue #8) ---

    TELEMETRY_SAMPLE_INTERVAL_S = 0.5  # low-rate sampling, independent of the 10Hz control/safety loops

    def start_telemetry_recording(self):
        """
        Begin periodically sampling per-drone telemetry into self.telemetry_log for
        later CSV export. Safe to call multiple times: restarts recording from empty.
        """
        self.stop_telemetry_recording()
        self.telemetry_log = []
        self.is_recording_telemetry = True
        self._telemetry_recording_task = asyncio.create_task(self._telemetry_recording_loop())

    def stop_telemetry_recording(self):
        """Stop periodic telemetry sampling. Already-recorded samples are kept for export."""
        self.is_recording_telemetry = False
        if self._telemetry_recording_task:
            self._telemetry_recording_task.cancel()
            self._telemetry_recording_task = None

    async def _telemetry_recording_loop(self):
        """Samples every drone's telemetry at TELEMETRY_SAMPLE_INTERVAL_S for post-flight CSV export."""
        try:
            while True:
                sample_time = time.time()
                for drone_id, tel in self.telemetry.items():
                    self.telemetry_log.append({
                        "timestamp": sample_time,
                        "drone_id": drone_id,
                        "x": tel.get("fleet_x", 0.0),
                        "y": tel.get("fleet_y", 0.0),
                        "z": tel.get("fleet_z", 0.0),
                        "battery_pct": tel.get("battery_percent", 0.0),
                        "mode": tel.get("flight_mode", "UNKNOWN"),
                        "connected": tel.get("connected", False),
                    })
                await asyncio.sleep(self.TELEMETRY_SAMPLE_INTERVAL_S)
        except asyncio.CancelledError:
            pass

    def export_telemetry_csv(self, filepath: str) -> int:
        """
        Writes all recorded telemetry samples (see self.telemetry_log) to a CSV file
        at filepath. Returns the number of rows written.
        """
        fieldnames = ["timestamp", "drone_id", "x", "y", "z", "battery_pct", "mode", "connected"]
        with open(filepath, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(self.telemetry_log)
        return len(self.telemetry_log)

    # --- Show-state persistence & resume (issue #4) ---

    STATE_SNAPSHOT_INTERVAL_S = 5.0  # periodic, not every 10Hz control tick

    def start_state_snapshotting(self):
        """Begin periodically persisting the active trajectory + playback position to disk."""
        self.stop_state_snapshotting()
        self._state_snapshot_task = asyncio.create_task(self._state_snapshot_loop())

    def stop_state_snapshotting(self):
        """Stop periodic snapshotting. Does not touch any file already on disk."""
        if self._state_snapshot_task:
            self._state_snapshot_task.cancel()
            self._state_snapshot_task = None

    async def _state_snapshot_loop(self):
        """Writes a show-state snapshot to disk every STATE_SNAPSHOT_INTERVAL_S while a show runs."""
        try:
            while True:
                await asyncio.sleep(self.STATE_SNAPSHOT_INTERVAL_S)
                self._write_state_snapshot()
        except asyncio.CancelledError:
            pass

    def _write_state_snapshot(self):
        if not self.active_trajectory or self.show_started_at is None:
            return
        try:
            save_show_state(
                self.state_path,
                self.active_trajectory,
                self.show_mode,
                self.show_started_at,
                self.telemetry,
            )
        except Exception as e:
            logging.error(f"Failed to persist show state to {self.state_path}: {e}")

    def load_pending_resume_state(self) -> dict | None:
        """
        Called on GCS startup. If a saved show exists on disk, loads it into
        `self.pending_resume_state` (for an operator-facing resume/discard
        prompt) and returns it. Does NOT touch `active_trajectory`, arm, or
        launch anything — that only happens if the operator explicitly calls
        `resume_pending_show()`.
        """
        self.pending_resume_state = load_show_state(self.state_path)
        return self.pending_resume_state

    def resume_pending_show(self) -> float:
        """
        Applies a previously-loaded pending resume state: restores the active
        trajectory and calculates how far into it playback had reached, but
        does not start playback, arm, or launch anything — that still
        requires an explicit operator action (e.g. /api/start-trajectory).

        Returns the elapsed playback time (seconds) the show had reached
        when it was last saved, for the caller/frontend to display.

        Raises ValueError if there is no pending resume state.
        """
        if not self.pending_resume_state:
            raise ValueError("No pending saved show state to resume.")

        state = self.pending_resume_state
        self.active_trajectory = state["trajectory"]
        self.show_mode = state["mode"]
        elapsed = max(0.0, state["saved_at"] - state["show_started_at"])
        self.pending_resume_state = None
        discard_show_state(self.state_path)
        return elapsed

    def discard_pending_show(self) -> None:
        """Discards a previously-loaded pending resume state without applying it."""
        self.pending_resume_state = None
        discard_show_state(self.state_path)

    async def _run_trajectory_loop(self, trajectory_data: dict, mode: str = "LOCAL", trigger_epoch_ms: float = 0.0):
        print(f"Starting trajectory playback loop in {mode} mode...")
        
        # Clock synchronization (millisecond precision time-of-day trigger delay)
        if trigger_epoch_ms > 0:
            import time
            curr_epoch_s = time.time()
            trigger_s = trigger_epoch_ms / 1000.0
            wait_s = trigger_s - curr_epoch_s
            if wait_s > 0:
                print(f"Clock Sync: Waiting {wait_s:.3f} seconds to trigger show at Epoch {trigger_s}...")
                await asyncio.sleep(wait_s)
                
        start_time = asyncio.get_event_loop().time()
        
        # Calculate overall duration
        max_time = 0.0
        for drone_id, wps in trajectory_data.items():
            if wps:
                max_time = max(max_time, wps[-1]["time"])
                
        print(f"Trajectory duration: {max_time} seconds.")
        R = 6378137.0 # Earth radius for GPS projection

        # Bound before the try so the finally block below can always read it —
        # an unexpected exception (e.g. a drone dropping out of self.telemetry
        # mid-tick, or a MAVSDK error from the setpoint gather) skips the
        # except/else branches entirely, and an unbound name here would mask
        # the real exception with an UnboundLocalError.
        cancelled = False

        try:
            # 10Hz loop rate
            while self.is_running_trajectory:
                current_time = asyncio.get_event_loop().time()
                elapsed = current_time - start_time
                
                if elapsed > max_time + 1.0:
                    print("Trajectory complete. Hovering at last waypoints...")
                    self.is_running_trajectory = False
                    break
                    
                # Calculate and send setpoints for each drone
                setpoint_tasks = []
                for drone_id, drone in self.drones.items():
                    if drone_id not in trajectory_data or not self.telemetry[drone_id]["connected"]:
                        continue
                        
                    wps = trajectory_data[drone_id]
                    # Calculate interpolated point
                    target = interpolate_waypoint(wps, elapsed)
                    
                    if target:
                        # Trajectory coordinates are expressed in the shared fleet frame
                        # (pad spacing baked in by the Studio generator). Subtract this
                        # drone's own pad offset to get back to its own local/home frame.
                        pad_x_off, pad_y_off = _pad_offset(drone_id)

                        if mode.upper() == "GLOBAL":
                            # GLOBAL MODE: Translate fleet-frame offsets using home GPS coordinates
                            home_lat = self.telemetry[drone_id]["home_lat"]
                            home_lon = self.telemetry[drone_id]["home_lon"]

                            if home_lat == 0.0 and home_lon == 0.0:
                                logging.error(
                                    f"Drone {drone_id}: home GPS not yet locked — skipping GLOBAL setpoint "
                                    f"this tick rather than using a fallback coordinate."
                                )
                                continue

                            rel_x = target["x"] - pad_x_off
                            rel_y = target["y"] - pad_y_off
                            dlat = rel_x / R
                            dlon = rel_y / (R * math.cos(math.radians(home_lat)))
                            lat_deg = home_lat + math.degrees(dlat)
                            lon_deg = home_lon + math.degrees(dlon)
                            alt_m = -target["z"] # convert NED Z down to altitude above home

                            setpoint_tasks.append(
                                drone.offboard.set_position_global(
                                    PositionGlobalYaw(
                                        lat_deg,
                                        lon_deg,
                                        alt_m,
                                        target["yaw"],
                                        PositionGlobalYaw.AltitudeType.REL_HOME
                                    )
                                )
                            )
                        else:
                            # LOCAL MODE: Send position, velocity, and acceleration NED (feed-forward)
                            # Convert fleet-frame trajectory coordinates to this drone's own
                            # local/home frame by subtracting its known pad offset (not the
                            # trajectory's first waypoint, which may not sit at the pad after
                            # a slot remap). Z has no pad offset (pads are flat), so it's used as-is.
                            local_x = target["x"] - pad_x_off
                            local_y = target["y"] - pad_y_off

                            pos = PositionNedYaw(local_x, local_y, target["z"], target["yaw"])
                            vel = VelocityNedYaw(target.get("vx", 0.0), target.get("vy", 0.0), target.get("vz", 0.0), target["yaw"])
                            acc = AccelerationNed(target.get("ax", 0.0), target.get("ay", 0.0), target.get("az", 0.0))
                            setpoint_tasks.append(
                                drone.offboard.set_position_velocity_acceleration_ned(pos, vel, acc)
                            )
                        
                if setpoint_tasks:
                    await asyncio.gather(*setpoint_tasks)
                    
                await asyncio.sleep(0.1)  # 10Hz
                
        except asyncio.CancelledError:
            # Cancellation comes from either stop_trajectory() (manual stop —
            # already discarded saved state itself before cancelling) or
            # shutdown() (GCS going away mid-show — deliberately leaves the
            # last snapshot on disk so a restart can offer to resume). Either
            # way, the finally block below must not discard state itself.
            print("Trajectory playback loop cancelled.")
            cancelled = True
        finally:
            self.is_running_trajectory = False
            # Natural completion doesn't go through stop_trajectory(), so stop
            # recording here too (stop_trajectory() already covers the manual-stop path).
            self.stop_telemetry_recording()
            if not cancelled:
                self.stop_state_snapshotting()
                discard_show_state(self.state_path)

    def verify_launch_geometry(self, trajectory_data: dict, tolerance_m: float = 0.5) -> dict:
        """
        Verifies that each connected drone is physically placed within the tolerance_m
        envelope of its expected starting position (first waypoint) before arming/takeoff.
        """
        results = {}
        all_passed = True
        
        # Remap first to match actual drone slots
        remapped = self.remap_trajectory_to_healthy_drones(trajectory_data)
        
        for drone_id, wps in remapped.items():
            if not wps:
                continue
            
            # First waypoint at t=0
            first_wp = wps[0]
            
            if drone_id not in self.telemetry or not self.telemetry[drone_id]["connected"]:
                results[str(drone_id)] = {"status": "DISCONNECTED", "msg": "Drone not connected."}
                all_passed = False
                continue
                
            tel = self.telemetry[drone_id]
            # Expected first-waypoint position, in the shared fleet frame.
            expected_x = first_wp["x"]
            expected_y = first_wp["y"]
            expected_z = -first_wp["z"] # convert NED down to height

            # Actual position, converted from this drone's own local/home frame
            # into the same fleet frame (see LAUNCH_PAD_SPACING_Y_M).
            curr_x = tel["fleet_x"]
            curr_y = tel["fleet_y"]
            curr_z = tel["fleet_z"]

            dist = ((curr_x - expected_x)**2 + (curr_y - expected_y)**2 + (curr_z - expected_z)**2)**0.5
            
            passed = dist <= tolerance_m
            if not passed:
                all_passed = False
                
            results[str(drone_id)] = {
                "status": "PASSED" if passed else "FAILED",
                "current_pos": [curr_x, curr_y, curr_z],
                "expected_pos": [expected_x, expected_y, expected_z],
                "distance_error_m": round(dist, 2),
                "msg": f"Placed at ({curr_x:.2f}, {curr_y:.2f}, {curr_z:.2f}) - expected ({expected_x:.2f}, {expected_y:.2f}, {expected_z:.2f}). Error: {dist:.2f}m"
            }
            
        return {
            "all_passed": all_passed,
            "drones": results
        }

    async def _proximity_monitor_loop(self, safety_limit: float = DEFAULT_LIMITS.min_separation_m):
        """
        Monitors spatial separation between all active/armed drones at 10Hz.
        Triggers emergency failsafe (RTL) if distance is breached.
        """
        logging.info("Proximity safety monitor active.")
        try:
            while True:
                # Find connected & armed drones
                active_ids = [
                    d_id for d_id, tel in self.telemetry.items()
                    if tel["connected"] and tel["armed"]
                ]
                
                if len(active_ids) < 2:
                    self.proximity_breaches.clear()
                else:
                    current_breaches = set()
                    new_breach_found = False
                    # Check distances between all pairs, in the shared fleet frame
                    # (consistent with verify_launch_geometry and the Studio-exported
                    # trajectory coordinates). Uses a cKDTree instead of an O(n^2)
                    # nested loop — same approach as taramandal-studio's validator.py,
                    # which stays fast as fleet size grows toward hundreds of drones.
                    coords = [
                        [self.telemetry[d_id]["fleet_x"], self.telemetry[d_id]["fleet_y"], self.telemetry[d_id]["fleet_z"]]
                        for d_id in active_ids
                    ]
                    tree = cKDTree(coords)
                    for i, j in tree.query_pairs(safety_limit):
                        id1, id2 = active_ids[i], active_ids[j]
                        pair = (min(id1, id2), max(id1, id2))
                        current_breaches.add(pair)
                        if not self.proximity_breaches.get(pair, False):
                            self.proximity_breaches[pair] = True
                            new_breach_found = True
                            dist = ((coords[i][0] - coords[j][0])**2 +
                                    (coords[i][1] - coords[j][1])**2 +
                                    (coords[i][2] - coords[j][2])**2)**0.5
                            logging.warning(f"🚨 PROXIMITY FAILSAFE: Drone {id1} and Drone {id2} are too close ({dist:.2f}m)!")

                    # Clear breach tracking for any pair that is no longer violating limits
                    for pair in list(self.proximity_breaches.keys()):
                        if self.proximity_breaches[pair] and pair not in current_breaches:
                            self.proximity_breaches[pair] = False
                            logging.info(f"Proximity breach between Drone {pair[0]} and Drone {pair[1]} cleared.")

                    # Trigger RTL once per tick (not inline per-pair), after the full scan completes.
                    if new_breach_found:
                        logging.warning("🚨 TRIGGERING EMERGENCY RTL FOR ALL VEHICLES.")
                        await self.trigger_rtl()
                                
                await asyncio.sleep(0.1)  # 10Hz
        except asyncio.CancelledError:
            logging.info("Proximity safety monitor stopped.")

    async def _geofence_monitor_loop(self):
        """
        Monitors each armed drone's distance from home (0, 0) at 10Hz.
        If horizontal distance exceeds geofence_radius, triggers RTL once per breach.
        Breach tracking resets when the drone disarms.
        """
        logging.info(f"Geofence monitor active (radius: {self.geofence_radius}m).")
        try:
            while True:
                for drone_id, tel in self.telemetry.items():
                    if not tel["connected"] or not tel["armed"]:
                        continue

                    x = tel["local_x"]
                    y = tel["local_y"]
                    z = tel["local_z"]

                    dist = (x**2 + y**2) ** 0.5           # horizontal distance from home
                    dist3d = (x**2 + y**2 + z**2) ** 0.5  # total 3D distance from home

                    already_breached = self.geofence_breaches.get(drone_id, False)

                    if dist > self.geofence_radius:
                        if not already_breached:
                            logging.warning(
                                f"🚧 GEOFENCE BREACH: Drone {drone_id} is {dist:.2f}m from home "
                                f"(3D: {dist3d:.2f}m) — limit is {self.geofence_radius}m. "
                                f"Triggering RTL."
                            )
                            self.geofence_breaches[drone_id] = True
                            await self.trigger_rtl()
                    else:
                        # Within bounds — clear breach flag if previously set
                        if already_breached:
                            self.geofence_breaches[drone_id] = False

                await asyncio.sleep(0.1)  # 10Hz
        except asyncio.CancelledError:
            logging.info("Geofence monitor stopped.")

    def remap_trajectory_to_healthy_drones(self, trajectory_data: dict) -> dict:
        """
        Re-assigns trajectory slots to currently healthy drones.

        A drone is healthy if it is:
          - connected
          - battery_percent > 20%
          - satellites >= 6

        Healthy drones are sorted by their physical drone_id (index).
        Trajectory slots (0, 1, 2, ...) are assigned in order to healthy drones.
        Extra trajectory slots beyond available healthy drones are dropped.
        Extra healthy drones beyond available slots are left idle.

        Returns a new dict keyed by actual drone IDs mapped to their remapped waypoints.
        """
        # Determine healthy drones sorted by physical index
        healthy_ids = sorted(
            [
                d_id for d_id, tel in self.telemetry.items()
                if tel["connected"]
                and tel["battery_percent"] > 20.0
                and tel["satellites"] >= 6
            ]
        )

        # Trajectory slots are integer keys (0, 1, 2, ...)
        slot_keys = sorted([k for k in trajectory_data.keys() if isinstance(k, int)])

        skipped_drones = [d_id for d_id in self.telemetry if d_id not in healthy_ids]
        if skipped_drones:
            logging.warning(
                f"Swarm re-mapper: Skipping unhealthy/disconnected drones: {skipped_drones}"
            )

        remapped: dict = {}
        for slot_index, slot_key in enumerate(slot_keys):
            if slot_index >= len(healthy_ids):
                logging.warning(
                    f"Swarm re-mapper: No healthy drone available for trajectory slot {slot_key} — dropping slot."
                )
                break
            actual_drone_id = healthy_ids[slot_index]
            remapped[actual_drone_id] = trajectory_data[slot_key]
            logging.info(
                f"Swarm re-mapper: Trajectory slot {slot_key} → Drone {actual_drone_id}"
            )

        idle_drones = healthy_ids[len(slot_keys):]
        if idle_drones:
            logging.info(
                f"Swarm re-mapper: Drones {idle_drones} have no trajectory slot — will remain idle."
            )

        return remapped
