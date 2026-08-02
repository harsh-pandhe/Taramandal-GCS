#!/usr/bin/env python3
"""
Taramandal Studio Mock - Programmatic Trajectory Generator
Generates collision-free, time-ordered multi-drone show waypoints.
"""

import argparse
import json
import math
import os
import sys

def generate_circle_trajectory(num_drones, duration, step_rate):
    total_steps = int(duration * step_rate)
    drones_wps = {d_id: [] for d_id in range(num_drones)}
    
    # Grid offset: launch pads separated by 2.0 meters on Y axis
    start_positions = {d_id: {"x": 0.0, "y": float(2 * d_id), "z": 0.0} for d_id in range(num_drones)}
    
    # Target heights: strict 0.7m separation (2.0m, 2.7m, 3.4m, ...)
    target_heights = {d_id: 2.0 + d_id * 0.7 for d_id in range(num_drones)}
    
    # Center of rotation circle in xy space
    center_x = 5.0
    center_y = float(num_drones - 1)  # centered on the middle of launch pads
    radius = 4.0
    
    for step in range(total_steps + 1):
        t = step / float(step_rate)
        
        # Takeoff phase: 0 to 5 seconds
        if t <= 5.0:
            ratio = t / 5.0
            for d_id in range(num_drones):
                # Ascend to target heights
                z_val = - (target_heights[d_id] * ratio)
                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": 0.0,
                    "y": start_positions[d_id]["y"],
                    "z": round(z_val, 2),
                    "yaw": 0.0
                })
                
        # Main choreography phase: 5 to 25 seconds
        elif t <= 25.0:
            choreo_duration = 20.0
            elapsed = t - 5.0
            ratio = elapsed / choreo_duration
            
            for d_id in range(num_drones):
                # Rotate along a circle
                angle_offset = (2.0 * math.pi * d_id) / num_drones
                angle = angle_offset + (2.0 * math.pi * ratio) # full 360 rotation
                
                # Blend coordinate path from launch pad to circle edge in the first 2 seconds of choreo
                blend_ratio = min(1.0, elapsed / 2.0)
                
                target_x = center_x + radius * math.cos(angle)
                target_y = center_y + radius * math.sin(angle)
                
                # Maintain vertical separation
                target_z = - target_heights[d_id]
                
                x_val = 0.0 + blend_ratio * (target_x - 0.0)
                y_val = start_positions[d_id]["y"] + blend_ratio * (target_y - start_positions[d_id]["y"])
                
                yaw_val = round(math.degrees(angle) % 360, 1)
                
                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": round(x_val, 2),
                    "y": round(y_val, 2),
                    "z": round(target_z, 2),
                    "yaw": yaw_val
                })
                
        # Return and Land phase: 25 to 30 seconds
        else:
            land_duration = duration - 25.0
            elapsed = t - 25.0
            ratio = elapsed / land_duration
            
            for d_id in range(num_drones):
                # Return to launch pad XY and slowly descend
                start_wp = drones_wps[d_id][-1]
                target_x = 0.0
                target_y = start_positions[d_id]["y"]
                
                # Descend to half height first, then to ground
                target_z = - (target_heights[d_id] * (1.0 - ratio))
                
                x_val = start_wp["x"] + ratio * (target_x - start_wp["x"])
                y_val = start_wp["y"] + ratio * (target_y - start_wp["y"])
                
                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": round(x_val, 2),
                    "y": round(y_val, 2),
                    "z": round(target_z, 2),
                    "yaw": 0.0
                })
                
    return drones_wps

def generate_helix_trajectory(num_drones, duration, step_rate):
    total_steps = int(duration * step_rate)
    drones_wps = {d_id: [] for d_id in range(num_drones)}
    
    start_positions = {d_id: {"x": 0.0, "y": float(2 * d_id), "z": 0.0} for d_id in range(num_drones)}
    target_heights = {d_id: 2.0 + d_id * 0.7 for d_id in range(num_drones)}
    
    center_x = 6.0
    center_y = float(num_drones - 1)
    radius = 5.0
    
    for step in range(total_steps + 1):
        t = step / float(step_rate)
        
        # Takeoff: 0 to 5s
        if t <= 5.0:
            ratio = t / 5.0
            for d_id in range(num_drones):
                z_val = - (target_heights[d_id] * ratio)
                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": 0.0,
                    "y": start_positions[d_id]["y"],
                    "z": round(z_val, 2),
                    "yaw": 0.0
                })
                
        # Helix motion: 5 to 25s
        elif t <= 25.0:
            choreo_duration = 20.0
            elapsed = t - 5.0
            ratio = elapsed / choreo_duration
            
            for d_id in range(num_drones):
                angle_offset = (2.0 * math.pi * d_id) / num_drones
                angle = angle_offset + (2.0 * math.pi * ratio * 1.5) # 1.5 rotations
                
                blend_ratio = min(1.0, elapsed / 2.0)
                
                # Heliical height: oscillates dynamically
                height_osc = 1.2 * math.sin(2.0 * math.pi * ratio + d_id)
                target_height = target_heights[d_id] + height_osc
                
                target_x = center_x + radius * math.cos(angle)
                target_y = center_y + radius * math.sin(angle)
                target_z = - max(1.5, target_height)  # stay at least 1.5m high
                
                x_val = blend_ratio * target_x
                y_val = start_positions[d_id]["y"] + blend_ratio * (target_y - start_positions[d_id]["y"])
                
                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": round(x_val, 2),
                    "y": round(y_val, 2),
                    "z": round(target_z, 2),
                    "yaw": round(math.degrees(angle) % 360, 1)
                })
                
        # Landing: 25 to 30s
        else:
            land_duration = duration - 25.0
            elapsed = t - 25.0
            ratio = elapsed / land_duration
            
            for d_id in range(num_drones):
                start_wp = drones_wps[d_id][-1]
                target_x = 0.0
                target_y = start_positions[d_id]["y"]
                target_z = - (target_heights[d_id] * (1.0 - ratio))
                
                x_val = start_wp["x"] + ratio * (target_x - start_wp["x"])
                y_val = start_wp["y"] + ratio * (target_y - start_wp["y"])
                
                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": round(x_val, 2),
                    "y": round(y_val, 2),
                    "z": round(target_z, 2),
                    "yaw": 0.0
                })
                
    return drones_wps

def save_json(drones_wps, output_path):
    data = {"drones": []}
    for d_id, wps in drones_wps.items():
        data["drones"].append({
            "id": d_id,
            "waypoints": wps
        })
    with open(output_path, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"Trajectory saved successfully as JSON to: {output_path}")

def save_csv(drones_wps, output_path):
    import csv
    with open(output_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["drone_id", "time", "x", "y", "z", "yaw"])
        for d_id, wps in drones_wps.items():
            for wp in wps:
                writer.writerow([
                    d_id,
                    wp["time"],
                    wp["x"],
                    wp["y"],
                    wp["z"],
                    wp["yaw"]
                ])
    print(f"Trajectory saved successfully as CSV to: {output_path}")

def main():
    parser = argparse.ArgumentParser(description="Taramandal Studio Trajectory Generator")
    parser.add_argument("-n", "--num-drones", type=int, default=3, help="Number of drones (default: 3)")
    parser.add_argument("-s", "--shape", choices=["circle", "helix"], default="circle", help="Choreography pattern (default: circle)")
    parser.add_argument("-d", "--duration", type=float, default=30.0, help="Duration in seconds (default: 30.0)")
    parser.add_argument("-r", "--rate", type=float, default=2.0, help="Waypoints per second (Hz) (default: 2.0)")
    parser.add_argument("-o", "--output", type=str, default="choreography.json", help="Output file path (default: choreography.json)")
    
    args = parser.parse_args()
    
    if args.num_drones < 1 or args.num_drones > 10:
        print("Error: Number of drones must be between 1 and 10.")
        sys.exit(1)
        
    print(f"Generating programmatic '{args.shape}' trajectory:")
    print(f"  Drones: {args.num_drones}")
    print(f"  Duration: {args.duration}s @ {args.rate}Hz")
    
    if args.shape == "helix":
        drones_wps = generate_helix_trajectory(args.num_drones, args.duration, args.rate)
    else:
        drones_wps = generate_circle_trajectory(args.num_drones, args.duration, args.rate)
        
    ext = os.path.splitext(args.output)[1].lower()
    if ext == ".csv":
        save_csv(drones_wps, args.output)
    else:
        save_json(drones_wps, args.output)

if __name__ == "__main__":
    main()
