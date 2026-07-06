import asyncio
import sys
from mavsdk import System
from mavsdk.offboard import PositionNedYaw, OffboardError

# UDP ports for the 3 drones: 14540, 14541, 14542
DRONE_PORTS = [14540, 14541, 14542]

# Targets altitudes in meters: 2.0m, 2.7m, 3.4m (strict 0.7m spatial separation)
ALTITUDES = [2.0, 2.7, 3.4]

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
            
    print(f"[Drone {index}] Waiting for global position estimate & GPS lock...")
    async for health in drone.telemetry.health():
        if health.is_global_position_ok and health.is_home_position_ok:
            print(f"[Drone {index}] GPS Lock and Home position OK!")
            break
            
    # Display battery telemetry
    async for battery in drone.telemetry.battery():
        print(f"[Drone {index}] Battery: {battery.remaining_percent * 100:.1f}%, Voltage: {battery.voltage_v:.2f}V")
        break
        
    print(f"[Drone {index}] Arming...")
    await drone.action.arm()
    
    print(f"[Drone {index}] Initializing offboard mode...")
    # Set initial setpoint (0, 0, 0)
    await drone.offboard.set_position_ned(PositionNedYaw(0.0, 0.0, 0.0, 0.0))
    
    try:
        await drone.offboard.start()
        print(f"[Drone {index}] Offboard mode started!")
    except OffboardError as e:
        print(f"[Drone {index}] Starting offboard mode failed: {e._result.result}")
        print(f"[Drone {index}] Disarming...")
        await drone.action.disarm()
        return None
        
    # Command to fly to target altitude (NED: Down is negative, so altitude 2.0m is -2.0)
    print(f"[Drone {index}] Flying to target hover altitude: {target_alt}m...")
    await drone.offboard.set_position_ned(PositionNedYaw(0.0, 0.0, -target_alt, 0.0))
    
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
