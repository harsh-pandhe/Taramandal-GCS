"""
Taramandal Studio - Blender Animation Exporter
Runs inside Blender's Python environment (bpy) to extract drone trajectories.
"""

import json
import math
import sys

try:
    import bpy
    BLENDER_ACTIVE = True
except ImportError:
    BLENDER_ACTIVE = False

def export_blender_scene(output_path="blender_trajectory.json") -> bool:
    if not BLENDER_ACTIVE:
        print("Error: Blender 'bpy' module is not active. Run this inside Blender.")
        return False
        
    scene = bpy.context.scene
    fps = scene.render.fps
    start_frame = scene.frame_start
    end_frame = scene.frame_end
    
    # Find all objects named Drone_*
    drone_objects = [obj for obj in scene.objects if obj.name.startswith("Drone_")]
    if not drone_objects:
        print("Warning: No objects found matching pattern 'Drone_*' (e.g., Drone_00, Drone_01)")
        return False
        
    choreo_data = {"drones": []}
    
    # Store initial frame
    original_frame = scene.frame_current
    
    try:
        for obj in drone_objects:
            # Parse drone ID
            try:
                d_id = int(obj.name.split("_")[1])
            except (ValueError, IndexError):
                d_id = len(choreo_data["drones"])
                
            waypoints = []
            
            for frame in range(start_frame, end_frame + 1):
                scene.frame_set(frame)
                
                # Get world position
                pos = obj.matrix_world.to_translation()
                # Get rotation in Euler angles (yaw is Z-axis rotation)
                rot = obj.matrix_world.to_euler()
                
                time_s = (frame - start_frame) / float(fps)
                
                # Blender maps coordinates:
                # X -> North/South (local X)
                # Y -> East/West (local Y)
                # Z -> Altitude (convert positive Blender Z to negative NED Z)
                z_val = - pos.z
                
                yaw_deg = math.degrees(rot.z) % 360
                
                waypoints.append({
                    "time": round(time_s, 2),
                    "x": round(pos.x, 2),
                    "y": round(pos.y, 2),
                    "z": round(z_val, 2),
                    "yaw": round(yaw_deg, 1)
                })
                
            choreo_data["drones"].append({
                "id": d_id,
                "waypoints": waypoints
            })
            
        with open(output_path, "w") as f:
            json.dump(choreo_data, f, indent=2)
            
        print(f"Blender animation exported successfully to: {output_path}")
        return True
    finally:
        # Restore frame
        scene.frame_set(original_frame)

if __name__ == "__main__":
    # Check if run with args inside Blender
    args = sys.argv
    if "--" in args:
        idx = args.index("--")
        out_file = args[idx + 1] if idx + 1 < len(args) else "blender_trajectory.json"
        export_blender_scene(out_file)
    else:
        export_blender_scene()
