import asyncio
import logging
from mavsdk import System
from mavsdk.offboard import PositionNedYaw, OffboardError

# Default target altitudes for basic spatial separation (0.7m steps)
DEFAULT_ALTITUDES = [2.0, 2.7, 3.4, 4.1, 4.8, 5.5]

class DroneManager:
    def __init__(self, ports=[14540, 14541, 14542]):
        self.ports = ports
        self.drones = {}  # drone_id -> System
        self.telemetry = {}  # drone_id -> status dict
        self._tasks = []
        self.trajectory_task = None
        self.is_running_trajectory = False
        self.geofence_radius = 30.0  # meters, horizontal distance from home (0,0)
        self.geofence_breaches = {}  # drone_id -> bool, tracks active geofence breaches
        self.proximity_breaches = {}  # tracks active proximity breaches per drone pair: (id1, id2) -> bool

    async def connect_all(self):
        """Connects to all drones concurrently."""
        logging.info(f"Connecting to {len(self.ports)} drones on ports: {self.ports}")
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
                "local_x": 0.0,
                "local_y": 0.0,
                "local_z": 0.0,
                "heading": 0.0
            }

            # Start telemetry loop for this drone
            self._tasks.append(asyncio.create_task(self._monitor_connection(drone_id)))
            self._tasks.append(asyncio.create_task(self._monitor_battery(drone_id)))
            self._tasks.append(asyncio.create_task(self._monitor_flight_mode(drone_id)))
            self._tasks.append(asyncio.create_task(self._monitor_gps(drone_id)))
            self._tasks.append(asyncio.create_task(self._monitor_position(drone_id)))
            self._tasks.append(asyncio.create_task(self._monitor_armed_state(drone_id)))
        except Exception as e:
            logging.error(f"Error during _connect_drone initialization for Drone {drone_id}: {e}")
            raise e

    async def shutdown(self):
        """Cleans up all background monitoring tasks and disarms drones safely."""
        logging.info("Shutting down DroneManager...")
        
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
                self.telemetry[drone_id]["battery_percent"] = round(battery.remaining_percent * 100, 1)
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
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error(f"Error in _monitor_gps for Drone {drone_id}: {e}")

    async def _monitor_position(self, drone_id):
        try:
            drone = self.drones[drone_id]
            async for pos in drone.telemetry.position_velocity_ned():
                self.telemetry[drone_id]["local_x"] = round(pos.position.north_m, 2)
                self.telemetry[drone_id]["local_y"] = round(pos.position.east_m, 2)
                # NED: convert Down (negative) back to Altitude (positive) for frontend display
                self.telemetry[drone_id]["local_z"] = round(-pos.position.down_m, 2)
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

    # --- Flight commands ---

    async def launch_sequence(self):
        """Arm, switch to offboard, and take off sequentially with 1-second spacing."""
        logging.info("Launching fleet sequentially...")
        for drone_id, drone in self.drones.items():
            if not self.telemetry[drone_id]["connected"]:
                logging.warning(f"Skipping Drone {drone_id}: Disconnected")
                continue
                
            try:
                # Arm
                try:
                    await drone.action.arm()
                    logging.info(f"Drone {drone_id} ARMED.")
                except Exception as e:
                    logging.error(f"Arming Drone {drone_id} failed: {e}")
                    continue
                    
                # Set initial position NED (0,0,0) before offboard
                try:
                    await drone.offboard.set_position_ned(PositionNedYaw(0.0, 0.0, 0.0, 0.0))
                except Exception as e:
                    logging.error(f"Setting initial position NED failed for Drone {drone_id}: {e}")
                    continue
                
                # Start offboard mode
                try:
                    await drone.offboard.start()
                    logging.info(f"Drone {drone_id} entered OFFBOARD mode.")
                except OffboardError as e:
                    logging.error(f"Offboard start failed for Drone {drone_id}: {e._result.result}")
                    await drone.action.disarm()
                    continue
                except Exception as e:
                    logging.error(f"Offboard start failed for Drone {drone_id}: {e}")
                    await drone.action.disarm()
                    continue
                    
                # Ascend to target altitude (spatial separation)
                alt = DEFAULT_ALTITUDES[drone_id] if drone_id < len(DEFAULT_ALTITUDES) else 3.0
                logging.info(f"Drone {drone_id} climbing to target hover altitude: {alt}m...")
                try:
                    await drone.offboard.set_position_ned(PositionNedYaw(0.0, 0.0, -alt, 0.0))
                except Exception as e:
                    logging.error(f"Climbing to target altitude failed for Drone {drone_id}: {e}")
                    continue
                    
            except Exception as e:
                logging.error(f"Unexpected error launching Drone {drone_id}: {e}")
                continue
            
            # 1-second delay between launches
            await asyncio.sleep(1.0)

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

    def start_trajectory(self, trajectory_data: dict):
        """Start playing back waypoints in a background asyncio task."""
        self.stop_trajectory()
        # Remap trajectory slots to currently healthy drones before playback
        remapped = self.remap_trajectory_to_healthy_drones(trajectory_data)
        self.is_running_trajectory = True
        self.trajectory_task = asyncio.create_task(self._run_trajectory_loop(remapped))

    def stop_trajectory(self):
        """Stop current trajectory playback."""
        self.is_running_trajectory = False
        if self.trajectory_task:
            self.trajectory_task.cancel()
            self.trajectory_task = None
            print("Trajectory playback stopped.")

    async def _run_trajectory_loop(self, trajectory_data: dict):
        print("Starting trajectory playback loop...")
        start_time = asyncio.get_event_loop().time()
        
        # Calculate overall duration
        max_time = 0.0
        for drone_id, wps in trajectory_data.items():
            if wps:
                max_time = max(max_time, wps[-1]["time"])
                
        print(f"Trajectory duration: {max_time} seconds.")
        
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
                    target = self._interpolate_waypoint(wps, elapsed)
                    
                    if target:
                        # Send position setpoint (NED Down is negative, so Z from trajectory is converted)
                        setpoint_tasks.append(
                            drone.offboard.set_position_ned(
                                PositionNedYaw(target["x"], target["y"], target["z"], target["yaw"])
                            )
                        )
                        
                if setpoint_tasks:
                    await asyncio.gather(*setpoint_tasks)
                    
                await asyncio.sleep(0.1)  # 10Hz
                
        except asyncio.CancelledError:
            print("Trajectory playback loop cancelled.")
        finally:
            self.is_running_trajectory = False

    def _interpolate_waypoint(self, waypoints: list, elapsed_time: float) -> dict:
        """Helper to lineary interpolate coordinates between waypoints based on elapsed time."""
        if not waypoints:
            return None
            
        # If before first waypoint
        if elapsed_time <= waypoints[0]["time"]:
            return waypoints[0]
            
        # If past last waypoint
        if elapsed_time >= waypoints[-1]["time"]:
            return waypoints[-1]
            
        # Find the segment
        for i in range(len(waypoints) - 1):
            wp_start = waypoints[i]
            wp_end = waypoints[i+1]
            if wp_start["time"] <= elapsed_time <= wp_end["time"]:
                # Interpolate
                duration = wp_end["time"] - wp_start["time"]
                if duration == 0:
                    return wp_start
                ratio = (elapsed_time - wp_start["time"]) / duration
                
                return {
                    "x": wp_start["x"] + ratio * (wp_end["x"] - wp_start["x"]),
                    "y": wp_start["y"] + ratio * (wp_end["y"] - wp_start["y"]),
                    "z": wp_start["z"] + ratio * (wp_end["z"] - wp_start["z"]),
                    "yaw": wp_start["yaw"] + ratio * (wp_end["yaw"] - wp_start["yaw"])
                }
                
        return waypoints[-1]

    async def _proximity_monitor_loop(self, safety_limit: float = 1.5):
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
                    # Check distances between all pairs
                    for i in range(len(active_ids)):
                        for j in range(i + 1, len(active_ids)):
                            id1 = active_ids[i]
                            id2 = active_ids[j]
                            pair = (min(id1, id2), max(id1, id2))
                            
                            t1 = self.telemetry[id1]
                            t2 = self.telemetry[id2]
                            
                            dist = ((t1["local_x"] - t2["local_x"])**2 + 
                                    (t1["local_y"] - t2["local_y"])**2 + 
                                    (t1["local_z"] - t2["local_z"])**2)**0.5
                                    
                            if dist < safety_limit:
                                current_breaches.add(pair)
                                if not self.proximity_breaches.get(pair, False):
                                    self.proximity_breaches[pair] = True
                                    logging.warning(f"🚨 PROXIMITY FAILSAFE: Drone {id1} and Drone {id2} are too close ({dist:.2f}m)!")
                                    logging.warning("🚨 TRIGGERING EMERGENCY RTL FOR ALL VEHICLES.")
                                    await self.trigger_rtl()
                                    
                    # Clear breach tracking for any pair that is no longer violating limits
                    for pair in list(self.proximity_breaches.keys()):
                        if self.proximity_breaches[pair] and pair not in current_breaches:
                            self.proximity_breaches[pair] = False
                            logging.info(f"Proximity breach between Drone {pair[0]} and Drone {pair[1]} cleared.")
                                
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
