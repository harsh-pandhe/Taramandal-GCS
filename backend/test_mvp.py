import asyncio
import sys
import os
from mavsdk import System
from mavsdk.offboard import PositionNedYaw, OffboardError, VelocityNedYaw, AccelerationNed

# UDP ports for the drones dynamically allocated (defaulting to 3)
NUM_DRONES = int(os.environ.get("NUM_DRONES", 3))
DRONE_PORTS = [14540 + i for i in range(NUM_DRONES)]

# Targets altitudes in meters (strict 0.7m spatial separation)
ALTITUDES = [2.0 + 0.7 * i for i in range(NUM_DRONES)]

async def run_drone_mvp(index, port, target_alt):
    print(f"[Drone {index}] Connecting to udpin://127.0.0.1:{port} (gRPC port {50051 + index})...")
    # Use distinct gRPC ports for each drone's mavsdk_server instance
    drone = System(port=50051 + index)
    await drone.connect(system_address=f"udpin://127.0.0.1:{port}")
    
    print(f"[Drone {index}] Waiting for connection...")
    async for state in drone.core.connection_state():
        if state.is_connected:
            print(f"[Drone {index}] Connected successfully!")
            break
            
    # Bypass magnetic and IMU calibration checks to prevent arming failure in simulation
    print(f"[Drone {index}] Setting parameters to bypass magnetic and IMU calibration check...")
    try:
        await drone.param.set_param_int("COM_ARM_MAG_STR", 0)
        await drone.param.set_param_int("EKF2_MAG_CHECK", 0)
        await drone.param.set_param_float("COM_ARM_IMU_ACC", 10.0)
        await drone.param.set_param_float("COM_ARM_IMU_GYR", 10.0)
    except Exception as e:
        print(f"[Drone {index}] Warning: Failed to set parameters: {e}")

    print(f"[Drone {index}] Waiting for local position estimate, GPS lock, and Armable status...")
    async for health in drone.telemetry.health():
        if health.is_local_position_ok and health.is_home_position_ok and health.is_armable:
            print(f"[Drone {index}] Local position estimate, Home position, and Armable status OK!")
            break
            
    # Display battery telemetry
    async for battery in drone.telemetry.battery():
        print(f"[Drone {index}] Battery: {battery.remaining_percent * 100:.1f}%, Voltage: {battery.voltage_v:.2f}V")
        break

    print(f"[Drone {index}] Arming...")
    await drone.action.arm()
    
    print(f"[Drone {index}] Initializing offboard mode...")
    # Set initial setpoint (0, 0, 0)
    await drone.offboard.set_position_velocity_acceleration_ned(
        PositionNedYaw(0.0, 0.0, 0.0, 0.0),
        VelocityNedYaw(0.0, 0.0, 0.0, 0.0),
        AccelerationNed(0.0, 0.0, 0.0)
    )
    
    try:
        await drone.offboard.start()
        print(f"[Drone {index}] Offboard mode started!")
    except OffboardError as e:
        print(f"[Drone {index}] Starting offboard mode failed: {e._result.result}")
        print(f"[Drone {index}] Disarming...")
        await drone.action.disarm()
        return None
        
    # Command to fly to target altitude (NED: Down is negative, so altitude target_alt m is -target_alt)
    print(f"[Drone {index}] Flying to target hover altitude: {target_alt}m...")
    pos = PositionNedYaw(0.0, 0.0, -target_alt, 0.0)
    vel = VelocityNedYaw(0.0, 0.0, 0.0, 0.0)
    acc = AccelerationNed(0.0, 0.0, 0.0)
    await drone.offboard.set_position_velocity_acceleration_ned(pos, vel, acc)
    
    return drone

async def main():
    print("====================================================")
    print("Starting MVP Multi-Drone Flight Command Sequence")
    print("====================================================")
    
    drones = []
    
    # Connect and launch drones sequentially with 1-second delay
    for i, port in enumerate(DRONE_PORTS):
        target_alt = ALTITUDES[i]
        try:
            drone = await run_drone_mvp(i, port, target_alt)
            if drone:
                drones.append((i, drone))
            print(f"Waiting 1 second before commanding next drone...")
            await asyncio.sleep(1.0)
        except Exception as e:
            print(f"Error launching Drone {i} on port {port}: {e}")
            
    if len(drones) < len(DRONE_PORTS):
        print(f"Warning: Only {len(drones)}/3 drones initialized successfully.")
        
    if not drones:
        print("Error: No drones successfully launched. Exiting.")
        sys.exit(1)
        
    print("\nAll launched drones hovering...")
    print("Maintaining vertical spatial separation for 10 seconds...")
    await asyncio.sleep(10.0)
    
    print("\n====================================================")
    print("FAILSAFE TRIGGER: Commanding all drones to RTL (Return to Launch)...")
    print("====================================================")
    
    rtl_tasks = []
    for index, drone in drones:
        async def stop_and_rtl(d_idx, d_obj):
            try:
                # Stop offboard mode to allow RTL action
                await d_obj.offboard.stop()
            except Exception as e:
                print(f"[Drone {d_idx}] Offboard stop warning: {e}")
            try:
                await d_obj.action.return_to_launch()
                print(f"[Drone {d_idx}] RTL Command Sent.")
            except Exception as e:
                print(f"[Drone {d_idx}] RTL failed to command: {e}")
                
        rtl_tasks.append(stop_and_rtl(index, drone))
        
    await asyncio.gather(*rtl_tasks)
    
    print("\nWaiting 15 seconds for RTL sequence to progress...")
    await asyncio.sleep(15.0)
    print("MVP sequence complete.")

if __name__ == "__main__":
    asyncio.run(main())
