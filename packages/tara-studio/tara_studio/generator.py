"""
Taramandal Studio - Trajectory Generation Library
"""

import math
import logging

from tara_core import MAX_FLEET_SIZE, altitude_layers, launch_grid


def _default_launch_state(
    num_drones: int, spawn_offsets: list | None, target_heights: list | None
) -> tuple[list, list]:
    """Fill in default launch pads and hover altitudes from the shared limits.

    The hover altitudes previously stepped by 0.7 m per drone, which is below
    both the design threshold and the 1.5 m the fleet aborts on. That was not
    theoretical: in a 3-drone circle the drones converge horizontally as they
    blend out of takeoff, leaving the vertical stagger as the *only* separation,
    and the show bottomed out at 0.77 m. Altitude steps now come from
    ``tara_core.altitude_layers``, which derives them from the safety limits.

    Default pad positions come from the SAME fixed-size grid
    (``tara_core.launch_grid(MAX_FLEET_SIZE)``) that
    ``tara_gcs.drone_manager._pad_offset`` uses, not a 1D line. Those two used
    to disagree past 32 drones — a line keeps every drone at x=0 forever,
    while the GCS's grid wraps into a new row at 32 columns — which would
    silently misplace any auto-generated show larger than that once it
    reached the GCS. Returns ``(x, y)`` pairs when defaulted.

    An explicitly-passed ``spawn_offsets`` (the Studio UI's per-drone Y-offset
    field) is honoured as-is for backward compatibility — a flat list of
    floats, x=0 — since that is a public API shape (studio/server.py's
    ``GenerateRequest.spawn_offsets``, the web app's ``spawnOffsets`` state).
    Only the *default* (no override given) case gets the corrected 2D grid.

    Both overrides are indexed per drone id by every generator, so a list
    shorter than ``num_drones`` used to fail with a bare ``IndexError`` out of
    the middle of a shape's waypoint loop — the API surfaced that as an opaque
    500 with no indication of which field was wrong. Since a designer only
    hand-places pads for a fleet they have already sized, a short list is a
    caller bug, not something to pad with defaults: silently filling the tail
    from the grid would hand back a show whose pads disagree with the ones the
    operator actually set. Both are therefore length-checked up front, and
    reported by name and by the lengths involved.

    Raises:
        ValueError: If ``num_drones`` is outside ``1..MAX_FLEET_SIZE``, or if a
            supplied override's length does not match ``num_drones``.
    """
    if not 1 <= num_drones <= MAX_FLEET_SIZE:
        raise ValueError(
            f"num_drones must be between 1 and {MAX_FLEET_SIZE}, got {num_drones}"
        )

    for name, override in (("spawn_offsets", spawn_offsets), ("target_heights", target_heights)):
        if override is not None and len(override) != num_drones:
            raise ValueError(
                f"{name} has {len(override)} entries but num_drones is {num_drones}; "
                f"supply exactly one per drone or omit it to use the default grid"
            )

    if spawn_offsets is None:
        spawn_offsets = launch_grid(MAX_FLEET_SIZE)[:num_drones]
    if target_heights is None:
        target_heights = altitude_layers(num_drones)
    return spawn_offsets, target_heights


#: Fraction of the landing window spent returning to the pad horizontally
#: before any descent begins. See _land_waypoint's docstring for why.
LAND_RETURN_FRACTION = 0.6


def _land_waypoint(d_id: int, ratio: float, start_positions: dict, target_heights: dict, land_entry: dict) -> dict:
    """Computes one drone's position during the landing window.

    Splits the window into two sequential sub-phases instead of blending
    horizontal return and vertical descent simultaneously:

      1. **Return** (first ``LAND_RETURN_FRACTION`` of the window): fly from
         the choreography's end position to this drone's own pad, holding
         the choreography's ending altitude.
      2. **Descend** (the rest): drop straight down to the ground, XY already
         fixed at the pad.

    Blending both at once let one drone's horizontal return path and
    another's converge in space at the same moment their altitude gap was
    also shrinking, which measured as low as 1.09m of total separation for
    the circle shape — under the 1.5m the fleet aborts on — even though the
    launch-pad grid itself is spaced >=2m apart. Returning to the
    already-safe pad grid *before* giving up altitude means the altitude
    collapse at touchdown always has that >=2m of horizontal separation to
    fall back on.

    Args:
        d_id: Drone index.
        ratio: Progress through the landing window, 0 to 1.
        start_positions: Each drone's pad ``{"x", "y"}``.
        target_heights: Each drone's hover altitude.
        land_entry: Each drone's ``{"x", "y", "z"}`` at the moment landing
            began (the choreography's last waypoint) — captured once, not
            re-read from the continuously-updating last-appended waypoint.

    Returns:
        ``{"x", "y", "z", "yaw"}``, unrounded.
    """
    return_ratio = min(1.0, ratio / LAND_RETURN_FRACTION)
    descend_ratio = max(0.0, (ratio - LAND_RETURN_FRACTION) / (1.0 - LAND_RETURN_FRACTION))

    entry = land_entry[d_id]
    target_x = start_positions[d_id]["x"]
    target_y = start_positions[d_id]["y"]

    x_val = entry["x"] + return_ratio * (target_x - entry["x"])
    y_val = entry["y"] + return_ratio * (target_y - entry["y"])
    z_val = entry["z"] * (1.0 - descend_ratio)

    return {"x": x_val, "y": y_val, "z": z_val, "yaw": 0.0}


def _pad_xy(pad) -> tuple[float, float]:
    """Extracts (x, y) from either pad representation `_default_launch_state` can return.

    A defaulted pad is an ``(x, y)`` tuple from ``launch_grid``; an explicitly
    passed ``spawn_offsets`` entry is a bare float (y-only, x=0) for backward
    compatibility with the existing Studio UI/API.
    """
    if isinstance(pad, (tuple, list)):
        return float(pad[0]), float(pad[1])
    return 0.0, float(pad)

def generate_circle(num_drones: int, duration: float, rate: float, spawn_offsets: list = None, target_heights: list = None) -> dict:
    total_steps = int(duration * rate)
    drones_wps = {d_id: [] for d_id in range(num_drones)}
    
    spawn_offsets, target_heights = _default_launch_state(
        num_drones, spawn_offsets, target_heights
    )
        
    start_positions = {d_id: {"x": _pad_xy(spawn_offsets[d_id])[0], "y": _pad_xy(spawn_offsets[d_id])[1], "z": 0.0} for d_id in range(num_drones)}
    target_heights = {d_id: float(target_heights[d_id]) for d_id in range(num_drones)}
    
    center_x = 5.0
    center_y = float(num_drones - 1)
    radius = 4.0
    
    land_entry = None  # captured once, at the moment landing begins
    for step in range(total_steps + 1):
        t = step / float(rate)
        
        # Takeoff: 0s to 5s
        if t <= 5.0:
            ratio = t / 5.0
            for d_id in range(num_drones):
                z_val = - (target_heights[d_id] * ratio)
                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": start_positions[d_id]["x"],
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
                
                x_val = start_positions[d_id]["x"] + blend_ratio * (target_x - start_positions[d_id]["x"])
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
            
            if land_entry is None:
                land_entry = {d_id: dict(drones_wps[d_id][-1]) for d_id in range(num_drones)}

            for d_id in range(num_drones):
                wp = _land_waypoint(d_id, ratio, start_positions, target_heights, land_entry)
                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": round(wp["x"], 2),
                    "y": round(wp["y"], 2),
                    "z": round(wp["z"], 2),
                    "yaw": wp["yaw"]
                })
                
    return {"drones": [{"id": d_id, "waypoints": wps} for d_id, wps in drones_wps.items()]}

def generate_helix(num_drones: int, duration: float, rate: float, spawn_offsets: list = None, target_heights: list = None) -> dict:
    total_steps = int(duration * rate)
    drones_wps = {d_id: [] for d_id in range(num_drones)}
    
    spawn_offsets, target_heights = _default_launch_state(
        num_drones, spawn_offsets, target_heights
    )
        
    start_positions = {d_id: {"x": _pad_xy(spawn_offsets[d_id])[0], "y": _pad_xy(spawn_offsets[d_id])[1], "z": 0.0} for d_id in range(num_drones)}
    target_heights = {d_id: float(target_heights[d_id]) for d_id in range(num_drones)}
    
    center_x = 6.0
    center_y = float(num_drones - 1)
    radius = 5.0
    
    land_entry = None  # captured once, at the moment landing begins
    for step in range(total_steps + 1):
        t = step / float(rate)
        
        if t <= 5.0:
            ratio = t / 5.0
            for d_id in range(num_drones):
                z_val = - (target_heights[d_id] * ratio)
                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": start_positions[d_id]["x"],
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
                
                x_val = start_positions[d_id]["x"] + blend_ratio * (target_x - start_positions[d_id]["x"])
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
            
            if land_entry is None:
                land_entry = {d_id: dict(drones_wps[d_id][-1]) for d_id in range(num_drones)}

            for d_id in range(num_drones):
                wp = _land_waypoint(d_id, ratio, start_positions, target_heights, land_entry)
                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": round(wp["x"], 2),
                    "y": round(wp["y"], 2),
                    "z": round(wp["z"], 2),
                    "yaw": wp["yaw"]
                })
                
    return {"drones": [{"id": d_id, "waypoints": wps} for d_id, wps in drones_wps.items()]}

def generate_heart(num_drones: int, duration: float, rate: float, spawn_offsets: list = None, target_heights: list = None) -> dict:
    total_steps = int(duration * rate)
    drones_wps = {d_id: [] for d_id in range(num_drones)}
    
    spawn_offsets, target_heights = _default_launch_state(
        num_drones, spawn_offsets, target_heights
    )
        
    start_positions = {d_id: {"x": _pad_xy(spawn_offsets[d_id])[0], "y": _pad_xy(spawn_offsets[d_id])[1], "z": 0.0} for d_id in range(num_drones)}
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

    land_entry = None  # captured once, at the moment landing begins
    for step in range(total_steps + 1):
        t = step / float(rate)
        
        # Takeoff
        if t <= 5.0:
            ratio = t / 5.0
            for d_id in range(num_drones):
                z_val = - (target_heights[d_id] * ratio)
                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": start_positions[d_id]["x"],
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
                
                x_val = start_positions[d_id]["x"] + blend_ratio * (target_x - start_positions[d_id]["x"])
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
            
            if land_entry is None:
                land_entry = {d_id: dict(drones_wps[d_id][-1]) for d_id in range(num_drones)}

            for d_id in range(num_drones):
                wp = _land_waypoint(d_id, ratio, start_positions, target_heights, land_entry)
                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": round(wp["x"], 2),
                    "y": round(wp["y"], 2),
                    "z": round(wp["z"], 2),
                    "yaw": wp["yaw"]
                })
                
    return {"drones": [{"id": d_id, "waypoints": wps} for d_id, wps in drones_wps.items()]}

def generate_cube(num_drones: int, duration: float, rate: float, spawn_offsets: list = None, target_heights: list = None) -> dict:
    # Position drones in a 3D grid layout
    total_steps = int(duration * rate)
    drones_wps = {d_id: [] for d_id in range(num_drones)}
    
    spawn_offsets, target_heights = _default_launch_state(
        num_drones, spawn_offsets, target_heights
    )
        
    start_positions = {d_id: {"x": _pad_xy(spawn_offsets[d_id])[0], "y": _pad_xy(spawn_offsets[d_id])[1], "z": 0.0} for d_id in range(num_drones)}
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
                
    land_entry = None  # captured once, at the moment landing begins
    for step in range(total_steps + 1):
        t = step / float(rate)
        
        # Takeoff
        if t <= 5.0:
            ratio = t / 5.0
            for d_id in range(num_drones):
                z_val = - (target_heights[d_id] * ratio)
                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": start_positions[d_id]["x"],
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
                
                x_val = start_positions[d_id]["x"] + blend_ratio * (target_x - start_positions[d_id]["x"])
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
            
            if land_entry is None:
                land_entry = {d_id: dict(drones_wps[d_id][-1]) for d_id in range(num_drones)}

            for d_id in range(num_drones):
                wp = _land_waypoint(d_id, ratio, start_positions, target_heights, land_entry)
                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": round(wp["x"], 2),
                    "y": round(wp["y"], 2),
                    "z": round(wp["z"], 2),
                    "yaw": wp["yaw"]
                })
                
    return {"drones": [{"id": d_id, "waypoints": wps} for d_id, wps in drones_wps.items()]}

def generate_spline(num_drones: int, duration: float, rate: float, spawn_offsets: list = None, target_heights: list = None) -> dict:
    total_steps = int(duration * rate)
    drones_wps = {d_id: [] for d_id in range(num_drones)}
    
    spawn_offsets, target_heights = _default_launch_state(
        num_drones, spawn_offsets, target_heights
    )
        
    start_positions = {d_id: {"x": _pad_xy(spawn_offsets[d_id])[0], "y": _pad_xy(spawn_offsets[d_id])[1], "z": 0.0} for d_id in range(num_drones)}
    target_heights = {d_id: float(target_heights[d_id]) for d_id in range(num_drones)}
    
    land_entry = None  # captured once, at the moment landing begins
    for step in range(total_steps + 1):
        t = step / float(rate)
        
        # Takeoff
        if t <= 5.0:
            ratio = t / 5.0
            for d_id in range(num_drones):
                z_val = - (target_heights[d_id] * ratio)
                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": start_positions[d_id]["x"],
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
                pad_x = start_positions[d_id]["x"]
                offset = start_positions[d_id]["y"]
                alt = target_heights[d_id]

                # Define cubic bezier control points, offset from this
                # drone's own pad x (was hardcoded from 0.0, which put every
                # drone's sweep on the same line regardless of its actual pad
                # column on the launch grid).
                p0 = (pad_x + 0.0, offset, -alt)
                p1 = (pad_x + 4.0, offset + 4.0, -alt - 2.0)
                p2 = (pad_x + 8.0, offset - 4.0, -alt + 1.5)
                p3 = (pad_x + 12.0, offset, -alt)
                
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
            
            if land_entry is None:
                land_entry = {d_id: dict(drones_wps[d_id][-1]) for d_id in range(num_drones)}

            for d_id in range(num_drones):
                wp = _land_waypoint(d_id, ratio, start_positions, target_heights, land_entry)
                drones_wps[d_id].append({
                    "time": round(t, 2),
                    "x": round(wp["x"], 2),
                    "y": round(wp["y"], 2),
                    "z": round(wp["z"], 2),
                    "yaw": wp["yaw"]
                })
                
    return {"drones": [{"id": d_id, "waypoints": wps} for d_id, wps in drones_wps.items()]}
