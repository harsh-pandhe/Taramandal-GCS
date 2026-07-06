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

    async def connect_all(self):
        """Connects to all drones concurrently."""
        print(f"Connecting to {len(self.ports)} drones on ports: {self.ports}")
        connect_tasks = []
        for i, port in enumerate(self.ports):
            connect_tasks.append(self._connect_drone(i, port))
        await asyncio.gather(*connect_tasks)

    async def _connect_drone(self, drone_id: int, port: int):
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

    async def shutdown(self):
        """Cleans up all background monitoring tasks."""
        print("Shutting down DroneManager...")
        if self.trajectory_task:
            self.trajectory_task.cancel()
        for task in self._tasks:
            task.cancel()
        self.drones.clear()

    # --- Telemetry loops ---

    async def _monitor_connection(self, drone_id):
        drone = self.drones[drone_id]
        async for state in drone.core.connection_state():
            self.telemetry[drone_id]["connected"] = state.is_connected

    async def _monitor_battery(self, drone_id):
        drone = self.drones[drone_id]
        async for battery in drone.telemetry.battery():
            self.telemetry[drone_id]["battery_percent"] = round(battery.remaining_percent * 100, 1)
            self.telemetry[drone_id]["battery_voltage"] = round(battery.voltage_v, 2)

    async def _monitor_flight_mode(self, drone_id):
        drone = self.drones[drone_id]
        async for fm in drone.telemetry.flight_mode():
            self.telemetry[drone_id]["flight_mode"] = str(fm)

    async def _monitor_gps(self, drone_id):
        drone = self.drones[drone_id]
        async for gps in drone.telemetry.gps_info():
            self.telemetry[drone_id]["satellites"] = gps.num_satellites
            # Arbitrary check for GPS lock
            self.telemetry[drone_id]["gps_lock"] = gps.num_satellites >= 6

    async def _monitor_position(self, drone_id):
        drone = self.drones[drone_id]
        async for pos in drone.telemetry.position_velocity_ned():
            self.telemetry[drone_id]["local_x"] = round(pos.position.north_m, 2)
            self.telemetry[drone_id]["local_y"] = round(pos.position.east_m, 2)
            # NED: convert Down (negative) back to Altitude (positive) for frontend display
            self.telemetry[drone_id]["local_z"] = round(-pos.position.down_m, 2)

    async def _monitor_armed_state(self, drone_id):
        drone = self.drones[drone_id]
        async for armed in drone.telemetry.armed():
            self.telemetry[drone_id]["armed"] = armed

    # --- Flight commands ---

    async def launch_sequence(self):
        """Arm, switch to offboard, and take off sequentially with 1-second spacing."""
        print("Launching fleet sequentially...")
        for drone_id, drone in self.drones.items():
            if not self.telemetry[drone_id]["connected"]:
                print(f"Skipping Drone {drone_id}: Disconnected")
                continue
                
            # Arm
            try:
                await drone.action.arm()
                print(f"Drone {drone_id} ARMED.")
            except Exception as e:
                print(f"Arming Drone {drone_id} failed: {e}")
                continue
                
            # Set initial position NED (0,0,0) before offboard
            await drone.offboard.set_position_ned(PositionNedYaw(0.0, 0.0, 0.0, 0.0))
            
            # Start offboard mode
            try:
                await drone.offboard.start()
                print(f"Drone {drone_id} entered OFFBOARD mode.")
            except OffboardError as e:
                print(f"Offboard start failed for Drone {drone_id}: {e}")
                await drone.action.disarm()
                continue
                
            # Ascend to target altitude (spatial separation)
            alt = DEFAULT_ALTITUDES[drone_id] if drone_id < len(DEFAULT_ALTITUDES) else 3.0
            print(f"Drone {drone_id} climbing to target hover altitude: {alt}m...")
            await drone.offboard.set_position_ned(PositionNedYaw(0.0, 0.0, -alt, 0.0))
            
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
        self.is_running_trajectory = True
        self.trajectory_task = asyncio.create_task(self._run_trajectory_loop(trajectory_data))

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
