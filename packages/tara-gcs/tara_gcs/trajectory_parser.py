import json
import io
import zipfile
from typing import Any
import pandas as pd

from tara_core import DEFAULT_LIMITS, MAX_FLEET_SIZE, SHOW_FORMAT_VERSION
from tara_core.show import Show
from tara_core.validate import collision_errors

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

def validate_trajectory_collisions(trajectory_data: dict, safety_distance: float = None) -> list:
    """
    Checks if any two drones drift within safety_distance during the trajectory.

    This is the last gate before a show can fly, so it enforces the hard runtime
    limit (`SafetyLimits.min_separation_m`) that the live proximity monitor
    aborts on — not the stricter design threshold the Studio validates against.
    A show certified by the Studio therefore always clears this check.

    The sweep itself lives in tara_core so the Studio, this uploader and the
    live monitor cannot drift apart again; it also replaces the previous
    hand-rolled O(n^2) pair loop with a cKDTree, which matters at 100 drones
    (~4,950 pairs per sample, ~300 samples per show).
    """
    return collision_errors(
        trajectory_data,
        limits=DEFAULT_LIMITS,
        separation=safety_distance,
    )

def _parse_json(content: str) -> dict:
    try:
        data = json.loads(content)
    except Exception as e:
        raise ValueError(f"Invalid JSON content: {e}")

    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object at top level, got {type(data).__name__}")

    # Canonical show format (issue #55): contains "drones" or "version".
    # Must be validated through Tara Core's canonical ShowContainer / Show.validate_and_load.
    # Malformed canonical shows MUST NOT be silently treated as legacy input.
    if "drones" in data or "version" in data:
        # Check explicit version compatibility first if version is present
        version = data.get("version")
        if version is not None and isinstance(version, int) and version > SHOW_FORMAT_VERSION:
            raise ValueError(
                f"Unsupported show format version {version}. "
                f"This GCS supports up to version {SHOW_FORMAT_VERSION}."
            )
        try:
            show = Show.validate_and_load(data)
        except Exception as e:
            raise ValueError(f"Show schema validation failed: {e}") from e

        return show.to_gcs()

    # Legacy direct dict representation: {0: [...], 1: [...]} or {"0": [...]}
    result = {}
    for k, v in data.items():
        if str(k).startswith("_"):
            continue
        try:
            drone_id = int(k)
            if drone_id < 0 or drone_id >= MAX_FLEET_SIZE:
                raise ValueError(f"Drone ID {drone_id} out of range [0, {MAX_FLEET_SIZE - 1}].")
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid drone ID key '{k}'. Must be an integer: {e}")

        if not isinstance(v, list):
            raise ValueError(f"Waypoint list for drone {drone_id} must be a list.")

        parsed_wps = []
        for wp in v:
            if not isinstance(wp, dict):
                raise ValueError(f"Waypoint for drone {drone_id} must be an object.")
            try:
                t_val = float(wp["time"])
                x_val = float(wp["x"])
                y_val = float(wp["y"])
                z_val = float(wp["z"])
                yaw_val = float(wp.get("yaw", 0.0))
            except KeyError as e:
                raise ValueError(f"Waypoint for drone {drone_id} is missing required key: {e}")
            except (ValueError, TypeError) as e:
                raise ValueError(f"Invalid numeric value in waypoint for drone {drone_id}: {e}")

            wp_dict: dict[str, Any] = {
                "time": t_val,
                "x": x_val,
                "y": y_val,
                "z": z_val,
                "yaw": yaw_val,
            }
            # Preserve LED channels if present
            for c in ("r", "g", "b", "w"):
                if c in wp and wp[c] is not None:
                    try:
                        wp_dict[c] = int(wp[c])
                    except (ValueError, TypeError):
                        pass
            # Preserve velocity if present
            for vel in ("vx", "vy", "vz"):
                if vel in wp and wp[vel] is not None:
                    try:
                        wp_dict[vel] = float(wp[vel])
                    except (ValueError, TypeError):
                        pass

            parsed_wps.append(wp_dict)

        sorted_wps = sorted(parsed_wps, key=lambda w: w["time"])
        result[drone_id] = sorted_wps

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
    # Guard CSV row limits (read max 100,001 rows to check overflow)
    df = pd.read_csv(io.StringIO(content), nrows=100001)
    if len(df) > 100000:
        raise ValueError("CSV exceeds maximum trajectory row limit (100,000 rows).")
        
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
        try:
            if pd.isna(drone_id):
                raise ValueError("Drone ID cannot be null/empty.")
            drone_id_int = int(float(drone_id))
            if drone_id_int < 0 or drone_id_int >= MAX_FLEET_SIZE:
                raise ValueError(f"Drone ID {drone_id_int} out of range [0, {MAX_FLEET_SIZE - 1}].")
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid drone ID value: {drone_id}. Must be a valid integer: {e}")
            
        # Sort group by time
        sorted_group = group.sort_values(by="time")
        result[drone_id_int] = []
        for _, row in sorted_group.iterrows():
            try:
                t_val = float(row["time"])
                x_val = float(row["x"])
                y_val = float(row["y"])
                z_val = float(row["z"])
                yaw_val = float(row["yaw"])
            except (ValueError, TypeError) as e:
                raise ValueError(f"Invalid coordinate or float value in row for Drone {drone_id_int}: {e}")
                
            wp_entry: dict[str, Any] = {
                "time": t_val,
                "x": x_val,
                "y": y_val,
                "z": z_val,
                "yaw": yaw_val
            }
            # Optional LED channels (as exported by Studio: r, g, b, and w if present)
            for c in ("r", "g", "b", "w"):
                if c in row and not pd.isna(row[c]):
                    try:
                        c_val = int(round(float(row[c])))
                        if 0 <= c_val <= 255:
                            wp_entry[c] = c_val
                    except (ValueError, TypeError):
                        pass
            # Optional velocity channels (vx, vy, vz)
            for vel in ("vx", "vy", "vz"):
                if vel in row and not pd.isna(row[vel]):
                    try:
                        wp_entry[vel] = float(row[vel])
                    except (ValueError, TypeError):
                        pass

            result[drone_id_int].append(wp_entry)
        
    return result
