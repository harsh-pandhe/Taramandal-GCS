"""Shared domain logic for the Taramandal drone-show platform.

Everything here is intentionally free of I/O, web framework and autopilot
dependencies so that the choreography studio, the ground control station and
the simulator can all agree on one definition of a show and one set of safety
limits.
"""

from .assignment import optimal_assignment, total_cost
from .geometry import (
    EARTH_RADIUS_M,
    geodetic_to_ned,
    interpolate_waypoint,
    jerk_from_positions,
    ned_to_geodetic,
    quintic_profile,
    quintic_segment,
    sweep_times,
)
from .layout import BASE_HOVER_ALT_M, MAX_FLEET_SIZE, altitude_layers, launch_grid
from .mission import (
    MissionHeader,
    ShowCommand,
    compile_mission,
    compute_show_id,
    decode_mission,
    decode_show_trigger,
    encode_show_trigger,
    mission_crc32,
)
from .proximity import close_pairs, min_separation
from .safety import DEFAULT_LIMITS, SafetyLimits
from .show import SHOW_FORMAT_VERSION, SHOW_SCHEMA_PATH, Show, ShowContainer, Waypoint
from .validate import MAX_REPORTED, altitude_errors, collision_errors, report_subset, validate_show

__all__ = [
    "BASE_HOVER_ALT_M",
    "MAX_FLEET_SIZE",
    "MAX_REPORTED",
    "DEFAULT_LIMITS",
    "EARTH_RADIUS_M",
    "MissionHeader",
    "SHOW_FORMAT_VERSION",
    "SHOW_SCHEMA_PATH",
    "SafetyLimits",
    "ShowCommand",
    "Show",
    "ShowContainer",
    "Waypoint",
    "altitude_errors",
    "altitude_layers",
    "close_pairs",
    "collision_errors",
    "compile_mission",
    "compute_show_id",
    "decode_mission",
    "decode_show_trigger",
    "encode_show_trigger",
    "geodetic_to_ned",
    "interpolate_waypoint",
    "jerk_from_positions",
    "launch_grid",
    "min_separation",
    "mission_crc32",
    "ned_to_geodetic",
    "optimal_assignment",
    "quintic_profile",
    "quintic_segment",
    "report_subset",
    "sweep_times",
    "total_cost",
    "validate_show",
]
