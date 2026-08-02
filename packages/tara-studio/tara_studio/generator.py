"""
Taramandal Studio - Trajectory Generation Library
"""

import math
import logging

def generate_circle(num_drones: int, duration: float, rate: float, spawn_offsets: list = None, target_heights: list = None) -> dict:
    total_steps = int(duration * rate)
    drones_wps = {d_id: [] for d_id in range(num_drones)}
    
    if spawn_offsets is None:
        spawn_offsets = [float(2.0 * d_id) for d_id in range(num_drones)]
    if target_heights is None:
        target_heights = [2.0 + d_id * 0.7 for d_id in range(num_drones)]
        
    start_positions = {d_id: {"x": 0.0, "y": float(spawn_offsets[d_id]), "z": 0.0} for d_id in range(num_drones)}
    target_heights = {d_id: float(target_heights[d_id]) for d_id in range(num_drones)}
    
    center_x = 5.0
    center_y = float(num_drones - 1)
    radius = 4.0
    
    for step in range(total_steps + 1):
        t = step / float(rate)
        
        # Takeoff: 0s to 5s
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
        # Choreography: 5s to 25s
        elif t <= 25.0:
            choreo_duration = 20.0
            elapsed = t - 5.0
            ratio = elapsed / choreo_duration
            
            for d_id in range(num_drones):
                angle_offset = (2.0 * math.pi * d_id) / num_drones
                angle = angle_offset + (2.0 * math.pi * ratio)
                
                blend_ratio = min(1.0, elapsed / 2.0)
                
                target_x = center_x + radius * math.cos(angle)
                target_y = center_y + radius * math.sin(angle)
                target_z = - target_heights[d_id]
                
                x_val = blend_ratio * target_x
                y_val = start_positions[d_id]["y"] + blend_ratio * (target_y - start_positions[d_id]["y"])
                yaw_val = round(math.degrees(angle) % 360, 1)
                
                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": round(x_val, 2),
                    "y": round(y_val, 2),
                    "z": round(target_z, 2),
                    "yaw": yaw_val
                })
        # Land: 25s to 30s
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
                
    return {"drones": [{"id": d_id, "waypoints": wps} for d_id, wps in drones_wps.items()]}

def generate_helix(num_drones: int, duration: float, rate: float, spawn_offsets: list = None, target_heights: list = None) -> dict:
    total_steps = int(duration * rate)
    drones_wps = {d_id: [] for d_id in range(num_drones)}
    
    if spawn_offsets is None:
        spawn_offsets = [float(2.0 * d_id) for d_id in range(num_drones)]
    if target_heights is None:
        target_heights = [2.0 + d_id * 0.7 for d_id in range(num_drones)]
        
    start_positions = {d_id: {"x": 0.0, "y": float(spawn_offsets[d_id]), "z": 0.0} for d_id in range(num_drones)}
    target_heights = {d_id: float(target_heights[d_id]) for d_id in range(num_drones)}
    
    center_x = 6.0
    center_y = float(num_drones - 1)
    radius = 5.0
    
    for step in range(total_steps + 1):
        t = step / float(rate)
        
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
        elif t <= 25.0:
            choreo_duration = 20.0
            elapsed = t - 5.0
            ratio = elapsed / choreo_duration
            
            for d_id in range(num_drones):
                angle_offset = (2.0 * math.pi * d_id) / num_drones
                angle = angle_offset + (2.0 * math.pi * ratio * 1.5)
                
                blend_ratio = min(1.0, elapsed / 2.0)
                height_osc = 1.2 * math.sin(2.0 * math.pi * ratio + d_id)
                target_height = target_heights[d_id] + height_osc
                
                target_x = center_x + radius * math.cos(angle)
                target_y = center_y + radius * math.sin(angle)
                target_z = - max(1.5, target_height)
                
                x_val = blend_ratio * target_x
                y_val = start_positions[d_id]["y"] + blend_ratio * (target_y - start_positions[d_id]["y"])
                
                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": round(x_val, 2),
                    "y": round(y_val, 2),
                    "z": round(target_z, 2),
                    "yaw": round(math.degrees(angle) % 360, 1)
                })
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
                
    return {"drones": [{"id": d_id, "waypoints": wps} for d_id, wps in drones_wps.items()]}

def generate_heart(num_drones: int, duration: float, rate: float, spawn_offsets: list = None, target_heights: list = None) -> dict:
    total_steps = int(duration * rate)
    drones_wps = {d_id: [] for d_id in range(num_drones)}
    
    if spawn_offsets is None:
        spawn_offsets = [float(2.0 * d_id) for d_id in range(num_drones)]
    if target_heights is None:
        target_heights = [2.0 + d_id * 0.7 for d_id in range(num_drones)]
        
    start_positions = {d_id: {"x": 0.0, "y": float(spawn_offsets[d_id]), "z": 0.0} for d_id in range(num_drones)}
    target_heights = {d_id: float(target_heights[d_id]) for d_id in range(num_drones)}
    
    center_x = 5.0
    center_y = float(num_drones - 1)
    
    # Heart parametric helper scaled down
    def get_heart_xy(theta):
        # theta is in [0, 2pi]
        # x = 16 * sin^3(t)
        # y = 13 * cos(t) - 5 * cos(2t) - 2 * cos(3t) - cos(4t)
        raw_x = 16.0 * (math.sin(theta) ** 3)
        raw_y = 13.0 * math.cos(theta) - 5.0 * math.cos(2.0*theta) - 2.0 * math.cos(3.0*theta) - math.cos(4.0*theta)
        
        # Scale down to typical arena bounds (approx -4 to +4)
        return raw_x * 0.25, raw_y * 0.25

    for step in range(total_steps + 1):
        t = step / float(rate)
        
        # Takeoff
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
        # Heart formation
        elif t <= 25.0:
            choreo_duration = 20.0
            elapsed = t - 5.0
            ratio = elapsed / choreo_duration
            
            for d_id in range(num_drones):
                # Spread out drones along heart perimeter
                theta = (2.0 * math.pi * d_id) / num_drones
                hx, hy = get_heart_xy(theta)
                
                # Blend entry
                blend_ratio = min(1.0, elapsed / 2.0)
                
                # Rotate the heart slightly over time
                rot_angle = 2.0 * math.pi * ratio * 0.25 # 90 degree spin
                target_x = center_x + (hx * math.cos(rot_angle) - hy * math.sin(rot_angle))
                target_y = center_y + (hx * math.sin(rot_angle) + hy * math.cos(rot_angle))
                target_z = - target_heights[d_id]
                
                x_val = blend_ratio * target_x
                y_val = start_positions[d_id]["y"] + blend_ratio * (target_y - start_positions[d_id]["y"])
                
                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": round(x_val, 2),
                    "y": round(y_val, 2),
                    "z": round(target_z, 2),
                    "yaw": round(math.degrees(rot_angle) % 360, 1)
                })
        # Landing
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
                
    return {"drones": [{"id": d_id, "waypoints": wps} for d_id, wps in drones_wps.items()]}

def generate_cube(num_drones: int, duration: float, rate: float, spawn_offsets: list = None, target_heights: list = None) -> dict:
    # Position drones in a 3D grid layout
    total_steps = int(duration * rate)
    drones_wps = {d_id: [] for d_id in range(num_drones)}
    
    if spawn_offsets is None:
        spawn_offsets = [float(2.0 * d_id) for d_id in range(num_drones)]
    if target_heights is None:
        target_heights = [2.0 + d_id * 0.7 for d_id in range(num_drones)]
        
    start_positions = {d_id: {"x": 0.0, "y": float(spawn_offsets[d_id]), "z": 0.0} for d_id in range(num_drones)}
    target_heights = {d_id: float(target_heights[d_id]) for d_id in range(num_drones)}
    
    # Compute 3D grid layouts
    # Let's map drone IDs to grid positions
    # Up to 8 drones fit in a 2x2x2 cube
    grid_coords = []
    side = int(math.ceil(num_drones ** (1/3.0)))
    if side < 2:
        side = 2
        
    for z in range(side):
        for y in range(side):
            for x in range(side):
                grid_coords.append((float(x * 2.5 + 4.0), float(y * 2.5 + 1.0), float(2.0 + z * 1.5)))
                
    for step in range(total_steps + 1):
        t = step / float(rate)
        
        # Takeoff
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
        # Form Cube
        elif t <= 25.0:
            choreo_duration = 20.0
            elapsed = t - 5.0
            ratio = elapsed / choreo_duration
            
            for d_id in range(num_drones):
                gx, gy, gz = grid_coords[d_id % len(grid_coords)]
                blend_ratio = min(1.0, elapsed / 2.0)
                
                # Slowly translate or rotate the cube
                offset_x = 1.0 * math.sin(2.0 * math.pi * ratio)
                target_x = gx + offset_x
                target_y = gy
                target_z = - gz
                
                x_val = blend_ratio * target_x
                y_val = start_positions[d_id]["y"] + blend_ratio * (target_y - start_positions[d_id]["y"])
                
                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": round(x_val, 2),
                    "y": round(y_val, 2),
                    "z": round(target_z, 2),
                    "yaw": 0.0
                })
        # Land
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
                
    return {"drones": [{"id": d_id, "waypoints": wps} for d_id, wps in drones_wps.items()]}

def generate_spline(num_drones: int, duration: float, rate: float, spawn_offsets: list = None, target_heights: list = None) -> dict:
    total_steps = int(duration * rate)
    drones_wps = {d_id: [] for d_id in range(num_drones)}
    
    if spawn_offsets is None:
        spawn_offsets = [float(2.0 * d_id) for d_id in range(num_drones)]
    if target_heights is None:
        target_heights = [2.0 + d_id * 0.7 for d_id in range(num_drones)]
        
    start_positions = {d_id: {"x": 0.0, "y": float(spawn_offsets[d_id]), "z": 0.0} for d_id in range(num_drones)}
    target_heights = {d_id: float(target_heights[d_id]) for d_id in range(num_drones)}
    
    for step in range(total_steps + 1):
        t = step / float(rate)
        
        # Takeoff
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
        # Choreography (Cubic Bezier curve sweep)
        elif t <= 25.0:
            choreo_duration = 20.0
            elapsed = t - 5.0
            ratio = elapsed / choreo_duration
            
            for d_id in range(num_drones):
                offset = start_positions[d_id]["y"]
                alt = target_heights[d_id]
                
                # Define cubic bezier control points
                p0 = (0.0, offset, -alt)
                p1 = (4.0, offset + 4.0, -alt - 2.0)
                p2 = (8.0, offset - 4.0, -alt + 1.5)
                p3 = (12.0, offset, -alt)
                
                # Symmetrical back-and-forth mapping of the spline trajectory
                t_bezier = ratio * 2.0 if ratio <= 0.5 else (1.0 - ratio) * 2.0
                
                # Cubic Bezier interpolation formula
                mt = 1.0 - t_bezier
                x = mt**3 * p0[0] + 3 * mt**2 * t_bezier * p1[0] + 3 * mt * t_bezier**2 * p2[0] + t_bezier**3 * p3[0]
                y = mt**3 * p0[1] + 3 * mt**2 * t_bezier * p1[1] + 3 * mt * t_bezier**2 * p2[1] + t_bezier**3 * p3[1]
                z = mt**3 * p0[2] + 3 * mt**2 * t_bezier * p1[2] + 3 * mt * t_bezier**2 * p2[2] + t_bezier**3 * p3[2]
                
                # Calculate simple yaw heading based on trajectory direction
                yaw = 90.0 if ratio <= 0.5 else 270.0
                
                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": round(x, 2),
                    "y": round(y, 2),
                    "z": round(z, 2),
                    "yaw": yaw
                })
        # Land
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
                
    return {"drones": [{"id": d_id, "waypoints": wps} for d_id, wps in drones_wps.items()]}
