"""Vectorized drone-fleet simulator.

Depends only on `tara_core` and `numpy` — no `mavsdk`, no PX4, no Gazebo.
`SimSystem` duck-types enough of `mavsdk.System` that `tara_gcs.DroneManager`
runs unchanged against either a real fleet or this one; see `system.py` for the
exact seam.
"""

from .fleet_state import FleetState, SimConfig
from .system import SimFleet, SimSystem

__all__ = ["FleetState", "SimConfig", "SimFleet", "SimSystem"]
