"""Turns an uploaded show file into the trajectory the GCS flies.

Every upload, whatever its format, goes through one gate (issue #55):

    canonical {"version", "drones": [...]}  ->  Show.validate_and_load
        ->  Show.to_gcs()  ->  sort by time  ->  collision_errors

so the GCS has no show schema of its own: what it accepts is exactly what
``tara_core.show.ShowContainer`` (the mirror of ``schemas/show_v1.json``)
accepts, the same check the Studio runs before it exports.

Studio-shaped JSON goes to the gate unchanged, so LED channels (r/g/b/w),
feed-forward velocities (vx/vy/vz) and unknown waypoint keys all survive. The
older formats — GCS-shaped JSON ``{id: [waypoints]}``, CSV and .skyc — are
thin adapters that only wrap their rows into the canonical shape. For those
alone, numpy scalars are cast to Python numbers and yaw is wrapped into
[0, 360), since files written before the schema existed routinely carry 360.0
or negative headings.
"""

import io
import json
import logging
import numbers
import zipfile

import pandas as pd
from pydantic import ValidationError

from tara_core import DEFAULT_LIMITS, SHOW_FORMAT_VERSION, Show
from tara_core.validate import collision_errors

logger = logging.getLogger("TaramandalGCS")

#: Schema errors listed in one rejection before the rest are summarised.
_MAX_REPORTED_ERRORS = 10

_LED_CHANNELS = ("r", "g", "b", "w")


def parse_trajectory_file(content: str, filename: str) -> dict:
    """
    Parses a trajectory file (JSON or CSV) and returns a structured dictionary
    mapping drone IDs to lists of time-ordered waypoints.

    Structure:
    {
        drone_id: [
            {"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0, ...},
            ...
        ]
    }

    For binary formats such as .skyc, use parse_trajectory_bytes instead.
    """
    try:
        if filename.endswith('.json'):
            return _parse_json(content)
        elif filename.endswith('.csv'):
            return _parse_csv(content)
        else:
            raise ValueError("Unsupported file format. Must be .json or .csv (use parse_trajectory_bytes for .skyc)")
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
            return _parse_skyc(content)
        # Decode as UTF-8 and fall back to text-based parser
        return parse_trajectory_file(content.decode('utf-8'), filename)
    except Exception as e:
        raise ValueError(f"Error parsing trajectory bytes ({filename}): {str(e)}")


def gate_show(canonical: dict) -> dict:
    """The one gate every show passes before the GCS will hold it.

    Args:
        canonical: A Studio-shaped show, ``{"version", "drones": [...]}``.
            A missing ``version`` is read as 1 (shows exported before the
            field existed), with a warning.

    Returns:
        The GCS-shaped trajectory ``{drone_id: [waypoints]}``, each drone's
        waypoints sorted by time, every key the file carried kept.

    Raises:
        ValueError: With an operator-readable reason — a newer show format,
            each schema violation by its field path (e.g.
            ``drones.3.waypoints.12.yaw``), an empty show, or a collision.
    """
    if not isinstance(canonical, dict):
        raise ValueError("A show file must be a JSON object.")

    version = canonical.get("version")
    if "version" not in canonical:
        logger.warning(
            f"Show file has no 'version'; reading it as show format {SHOW_FORMAT_VERSION}."
        )
        canonical = {"version": SHOW_FORMAT_VERSION, **canonical}
    elif isinstance(version, int) and not isinstance(version, bool) and version > SHOW_FORMAT_VERSION:
        # Checked before the schema: a newer format may legitimately fail
        # this version's rules, and "update the GCS" is the useful answer.
        raise ValueError(
            f"Show format version {version} is newer than this GCS understands "
            f"(up to {SHOW_FORMAT_VERSION}). Update the GCS, or re-export the show "
            f"from a Studio that writes version {SHOW_FORMAT_VERSION}."
        )

    try:
        show = Show.validate_and_load(canonical)
    except ValidationError as e:
        raise ValueError(_describe_schema_errors(e)) from None

    if not show.drones:
        raise ValueError("Show has no drones.")
    empty = sorted(drone_id for drone_id, wps in show.drones.items() if not wps)
    if empty:
        raise ValueError(f"Drone(s) {empty} have no waypoints.")

    trajectory = {
        drone_id: sorted(wps, key=lambda w: w["time"])
        for drone_id, wps in show.to_gcs().items()
    }

    errors = validate_trajectory_collisions(trajectory)
    if errors:
        raise ValueError("Trajectory safety check failed:\n" + "\n".join(errors))
    return trajectory


def gate_trajectory(trajectory: dict) -> dict:
    """:func:`gate_show` for a GCS-shaped trajectory this GCS produced itself
    (e.g. a show-state snapshot being resumed). Unlike the legacy file
    adapters nothing is cast or wrapped: the trajectory was gated once
    already, so anything that now fails was changed since.
    """
    return gate_show({
        "version": SHOW_FORMAT_VERSION,
        "drones": [{"id": drone_id, "waypoints": wps} for drone_id, wps in trajectory.items()],
    })


def _describe_schema_errors(error: ValidationError) -> str:
    lines = []
    for err in error.errors(include_url=False, include_input=False, include_context=False):
        where = ".".join(str(part) for part in err["loc"]) or "show"
        lines.append(f"  {where}: {err['msg']}")
    shown = lines[:_MAX_REPORTED_ERRORS]
    if len(lines) > len(shown):
        shown.append(f"  (+{len(lines) - len(shown)} more)")
    return "Show does not match the show schema (show_v1):\n" + "\n".join(shown)


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


# ---------------------------------------------------------------- adapters


def _python_number(value):
    """A numpy scalar as the plain Python int/float it holds; anything else unchanged."""
    if hasattr(value, "item") and isinstance(value, numbers.Number):
        return value.item()
    return value


def _legacy_waypoint(wp) -> dict:
    """Prepares one waypoint from a pre-schema format for the gate.

    Only normalises; it never fills in or drops a field, so a waypoint that is
    still wrong afterwards is reported by the gate with its field path.
    """
    if not isinstance(wp, dict):
        return wp
    out = {k: _python_number(v) for k, v in wp.items()}
    yaw = out.get("yaw")
    if isinstance(yaw, numbers.Real) and not isinstance(yaw, bool):
        yaw = float(yaw) % 360.0
        # -1e-17 % 360.0 rounds to 360.0, which the schema's [0, 360) refuses.
        out["yaw"] = 0.0 if yaw >= 360.0 else yaw
    return out


def _legacy_canonical(drones: dict, version=SHOW_FORMAT_VERSION) -> dict:
    """Wraps a GCS-shaped ``{id: [waypoints]}`` dict into the canonical shape."""
    return {
        "version": version,
        "drones": [
            {
                "id": drone_id,
                "waypoints": [_legacy_waypoint(wp) for wp in wps] if isinstance(wps, list) else wps,
            }
            for drone_id, wps in drones.items()
        ],
    }


def _parse_json(content: str) -> dict:
    data = json.loads(content)
    if isinstance(data, dict) and "drones" in data:
        # The Studio's own show format: the gate sees it exactly as written.
        return gate_show(data)
    if not isinstance(data, dict):
        raise ValueError("A show file must be a JSON object.")

    # Legacy GCS shape {"0": [waypoints], ...}. As in Show.from_any,
    # underscore keys are metadata (e.g. _geofence), not drones.
    drones = {}
    for k, v in data.items():
        if k == "version" or str(k).startswith("_"):
            continue
        try:
            drone_id = int(k)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid drone ID key '{k}'. Must be an integer: {e}")
        if not isinstance(v, list):
            raise ValueError(f"Waypoint list for drone {drone_id} must be a list.")
        if drone_id in drones:
            # "1" and "01" are both drone 1; the dict would keep only one path.
            raise ValueError(f"duplicate drone id(s): [{drone_id}]")
        drones[drone_id] = v
    return gate_show(_legacy_canonical(drones, data.get("version", SHOW_FORMAT_VERSION)))


def _read_skyc(content: bytes) -> dict:
    """
    Reads a Skybrush .skyc choreography file (ZIP archive containing show.json).

    The .skyc ZIP structure:
      show.json   (may be at root or inside a single sub-folder)

    show.json trajectory points format:
      [time_seconds, [x, y, z], [tangent_in or color]]

    Returns the GCS-shaped trajectory dict:
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


def _parse_skyc(content: bytes) -> dict:
    return gate_show(_legacy_canonical(_read_skyc(content)))


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

    # Studio's CSV export carries r,g,b when the show has light data; w is
    # read too when present. An empty cell leaves that channel out.
    led_cols = [c for c in _LED_CHANNELS if c in df.columns]

    drones = {}
    # Group by drone_id
    grouped = df.groupby("drone_id")
    for drone_id, group in grouped:
        try:
            if pd.isna(drone_id):
                raise ValueError("Drone ID cannot be null/empty.")
            drone_id_int = int(float(drone_id))
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid drone ID value: {drone_id}. Must be a valid integer: {e}")

        # Sort group by time
        sorted_group = group.sort_values(by="time")
        drones[drone_id_int] = []
        for _, row in sorted_group.iterrows():
            try:
                wp = {
                    "time": float(row["time"]),
                    "x": float(row["x"]),
                    "y": float(row["y"]),
                    "z": float(row["z"]),
                    "yaw": float(row["yaw"]),
                }
            except (ValueError, TypeError) as e:
                raise ValueError(f"Invalid coordinate or float value in row for Drone {drone_id_int}: {e}")
            for channel in led_cols:
                value = row[channel]
                if pd.isna(value):
                    continue
                value = _python_number(value)
                # pandas reads an int column holding any blank as float; a
                # whole-number float is that int. A fractional one is left
                # for the schema to refuse rather than silently truncated.
                if isinstance(value, float) and value.is_integer():
                    value = int(value)
                wp[channel] = value
            drones[drone_id_int].append(wp)

    return gate_show(_legacy_canonical(drones))
