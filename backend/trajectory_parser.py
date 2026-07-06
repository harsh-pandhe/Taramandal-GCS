import json
import io
import zipfile
import pandas as pd

def parse_trajectory_file(content: str, filename: str) -> dict:
    """
    Parses a trajectory file (JSON or CSV) and returns a structured dictionary
    mapping drone IDs to lists of time-ordered waypoints.
    
    Structure:
    {
        drone_id: [
            {"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
            ...
        ]
    }

    For binary formats such as .skyc, use parse_trajectory_bytes instead.
    """
    try:
        if filename.endswith('.json'):
            parsed = _parse_json(content)
        elif filename.endswith('.csv'):
            parsed = _parse_csv(content)
        else:
            raise ValueError("Unsupported file format. Must be .json or .csv (use parse_trajectory_bytes for .skyc)")
            
        # Run pre-flight space-time collision validation
        collision_errors = validate_trajectory_collisions(parsed)
        if collision_errors:
            raise ValueError("Trajectory safety check failed:\n" + "\n".join(collision_errors))
            
        return parsed
    except Exception as e:
        raise ValueError(f"Error parsing trajectory file: {str(e)}")


def parse_trajectory_bytes(content: bytes, filename: str) -> dict:
    """
    Unified entry point that handles both binary and text trajectory formats.

    Supported formats:
      - .skyc  : Skybrush choreography ZIP archive (binary)
      - .json  : JSON drone waypoint file (text)
      - .csv   : CSV drone waypoint file (text)

    Returns a structured dict mapping drone IDs to lists of time-ordered waypoints.
    """
    try:
        if filename.endswith('.skyc'):
            parsed = _parse_skyc(content)
        else:
            # Decode as UTF-8 and fall back to text-based parser
            content_str = content.decode('utf-8')
            return parse_trajectory_file(content_str, filename)

        # Run pre-flight space-time collision validation
        collision_errors = validate_trajectory_collisions(parsed)
        if collision_errors:
            raise ValueError("Trajectory safety check failed:\n" + "\n".join(collision_errors))

        return parsed
    except Exception as e:
        raise ValueError(f"Error parsing trajectory bytes ({filename}): {str(e)}")

def validate_trajectory_collisions(trajectory_data: dict, safety_distance: float = 1.5) -> list:
    """
    Checks if any two drones drift within safety_distance during the trajectory.
    """
    errors = []
    max_time = 0.0
    for drone_id, wps in trajectory_data.items():
        if wps:
            max_time = max(max_time, wps[-1]["time"])
            
    # Sample path coordinates at 10Hz steps
    t = 0.0
    while t <= max_time + 0.1:
        positions = {}
        for drone_id, wps in trajectory_data.items():
            # Interpolate position at time t
            if not wps:
                continue
            if t <= wps[0]["time"]:
                positions[drone_id] = wps[0]
            elif t >= wps[-1]["time"]:
                positions[drone_id] = wps[-1]
            else:
                for i in range(len(wps) - 1):
                    wp_start = wps[i]
                    wp_end = wps[i+1]
                    if wp_start["time"] <= t <= wp_end["time"]:
                        duration = wp_end["time"] - wp_start["time"]
                        ratio = (t - wp_start["time"]) / duration if duration > 0 else 0
                        positions[drone_id] = {
                            "x": wp_start["x"] + ratio * (wp_end["x"] - wp_start["x"]),
                            "y": wp_start["y"] + ratio * (wp_end["y"] - wp_start["y"]),
                            "z": wp_start["z"] + ratio * (wp_end["z"] - wp_start["z"])
                        }
                        break
                        
        # Check distances between all active pairs
        drone_ids = list(positions.keys())
        for i in range(len(drone_ids)):
            for j in range(i + 1, len(drone_ids)):
                id1 = drone_ids[i]
                id2 = drone_ids[j]
                p1 = positions[id1]
                p2 = positions[id2]
                
                dist = ((p1["x"] - p2["x"])**2 + (p1["y"] - p2["y"])**2 + (p1["z"] - p2["z"])**2)**0.5
                if dist < safety_distance:
                    errors.append(
                        f"Collision risk at {t:.1f}s: Drone {id1} and Drone {id2} are separated by {dist:.2f}m (limit: {safety_distance}m)"
                    )
                    # Cap errors list
                    if len(errors) >= 3:
                        return errors
        t += 0.1
        
    return errors

def _parse_json(content: str) -> dict:
    data = json.loads(content)
    result = {}
    
    if "drones" in data:
        for drone_data in data["drones"]:
            drone_id = int(drone_data.get("id", 0))
            waypoints = drone_data.get("waypoints", [])
            # Sort waypoints by time
            sorted_wps = sorted(waypoints, key=lambda w: float(w.get("time", 0.0)))
            result[drone_id] = [
                {
                    "time": float(wp.get("time", 0.0)),
                    "x": float(wp.get("x", 0.0)),
                    "y": float(wp.get("y", 0.0)),
                    "z": float(wp.get("z", 0.0)),
                    "yaw": float(wp.get("yaw", 0.0))
                }
                for wp in sorted_wps
            ]
    else:
        # Fallback to direct dict representation if structured differently
        for k, v in data.items():
            try:
                drone_id = int(k)
                sorted_wps = sorted(v, key=lambda w: float(w.get("time", 0.0)))
                result[drone_id] = [
                    {
                        "time": float(wp.get("time", 0.0)),
                        "x": float(wp.get("x", 0.0)),
                        "y": float(wp.get("y", 0.0)),
                        "z": float(wp.get("z", 0.0)),
                        "yaw": float(wp.get("yaw", 0.0))
                    }
                    for wp in sorted_wps
                ]
            except ValueError:
                continue
                
    return result


def _parse_skyc(content: bytes) -> dict:
    """
    Parses a Skybrush .skyc choreography file (ZIP archive containing show.json).

    The .skyc ZIP structure:
      show.json   (may be at root or inside a single sub-folder)

    show.json trajectory points format:
      [time_seconds, [x, y, z], [tangent_in or color]]

    Returns the standard trajectory dict:
      {
          drone_id (int): [
              {"time": float, "x": float, "y": float, "z": float, "yaw": 0.0},
              ...
          ]
      }
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content), 'r') as zf:
            # Locate show.json — could be at root or one level deep
            namelist = zf.namelist()
            show_json_path = None
            for name in namelist:
                # Match "show.json" or "*/show.json"
                if name == 'show.json' or name.endswith('/show.json'):
                    show_json_path = name
                    break

            if show_json_path is None:
                raise ValueError(
                    f"No 'show.json' found inside .skyc archive. "
                    f"Files present: {namelist}"
                )

            with zf.open(show_json_path) as f:
                show_data = json.loads(f.read().decode('utf-8'))

    except zipfile.BadZipFile:
        raise ValueError("The .skyc file is not a valid ZIP archive.")

    # Navigate to swarm.drones list
    try:
        drones_list = show_data["swarm"]["drones"]
    except (KeyError, TypeError) as e:
        raise ValueError(f"show.json is missing expected 'swarm.drones' key: {e}")

    result: dict = {}
    for drone_index, drone_data in enumerate(drones_list):
        try:
            traj = drone_data["trajectory"]
            points = traj.get("points", [])
        except (KeyError, TypeError):
            # Drone entry has no trajectory — assign empty waypoint list
            result[drone_index] = []
            continue

        waypoints = []
        for point in points:
            # Each point: [time_seconds, [x, y, z], [tangent/color]]
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            time_s = float(point[0])
            coords = point[1]
            if not isinstance(coords, (list, tuple)) or len(coords) < 3:
                continue
            waypoints.append({
                "time": time_s,
                "x": float(coords[0]),
                "y": float(coords[1]),
                "z": float(coords[2]),
                "yaw": 0.0  # .skyc uses light color not yaw; default to 0
            })

        # Ensure waypoints are time-sorted
        waypoints.sort(key=lambda w: w["time"])
        result[drone_index] = waypoints

    return result


def _parse_csv(content: str) -> dict:
    df = pd.read_csv(io.StringIO(content))
    
    # Required columns validation (case-insensitive)
    required_cols = {"drone_id", "time", "x", "y", "z", "yaw"}
    df.columns = [col.lower().strip() for col in df.columns]
    
    if not required_cols.issubset(df.columns):
        # Let's check for variations
        col_map = {}
        for col in df.columns:
            if "drone" in col or "id" in col:
                col_map[col] = "drone_id"
            elif "time" in col:
                col_map[col] = "time"
            elif col == "x" or col == "y" or col == "z" or col == "yaw":
                col_map[col] = col
        df = df.rename(columns=col_map)
        
    # Re-verify
    if not required_cols.issubset(df.columns):
        raise ValueError(f"CSV is missing one or more required columns: {required_cols}. Found: {list(df.columns)}")
        
    result = {}
    # Group by drone_id
    grouped = df.groupby("drone_id")
    for drone_id, group in grouped:
        drone_id_int = int(drone_id)
        # Sort group by time
        sorted_group = group.sort_values(by="time")
        result[drone_id_int] = [
            {
                "time": float(row["time"]),
                "x": float(row["x"]),
                "y": float(row["y"]),
                "z": float(row["z"]),
                "yaw": float(row["yaw"])
            }
            for _, row in sorted_group.iterrows()
        ]
        
    return result
