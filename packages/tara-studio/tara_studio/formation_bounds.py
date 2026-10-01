"""Formation bounds and geometric geofence constraint checking.

Enforces the project-wide cylindrical geofence envelope:
  * Horizontal distance from origin: r = sqrt(x^2 + y^2) <= geofence_radius_m (default: 30.0m)
  * Vertical envelope: 0.0m <= h <= max_altitude_m (default: 120.0m), where h = -z (NED)
  * Minimum separation: pairwise distance >= min_separation_m (default: 1.5m)

Prevents formations (rings, spheres, text, meshes) from exceeding physical
containment volumes or silently compressing below safe rotor clearance.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from tara_core import DEFAULT_LIMITS, SafetyLimits


@dataclass(frozen=True)
class FormationBounds:
    """Axis-aligned and radial bounds of a 3D formation point cloud."""

    min_x: float
    max_x: float
    min_y: float
    max_y: float
    min_z: float
    max_z: float
    max_radius_m: float
    max_altitude_m: float
    min_altitude_m: float
    center_x: float
    center_y: float
    center_z: float

    @classmethod
    def from_points(cls, points: Sequence[dict | tuple | list]) -> "FormationBounds":
        """Calculate spatial and radial bounds for a point set."""
        if not points:
            return cls(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

        xs, ys, zs = [], [], []
        max_r = 0.0
        for p in points:
            if isinstance(p, dict):
                x, y, z = float(p["x"]), float(p["y"]), float(p["z"])
            else:
                x, y, z = float(p[0]), float(p[1]), float(p[2])
            xs.append(x)
            ys.append(y)
            zs.append(z)
            max_r = max(max_r, math.hypot(x, y))

        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        min_z, max_z = min(zs), max(zs)

        # In NED frame: altitude above ground h = -z
        max_alt = -min_z
        min_alt = -max_z

        return cls(
            min_x=round(min_x, 3),
            max_x=round(max_x, 3),
            min_y=round(min_y, 3),
            max_y=round(max_y, 3),
            min_z=round(min_z, 3),
            max_z=round(max_z, 3),
            max_radius_m=round(max_r, 3),
            max_altitude_m=round(max_alt, 3),
            min_altitude_m=round(min_alt, 3),
            center_x=round((min_x + max_x) / 2.0, 3),
            center_y=round((min_y + max_y) / 2.0, 3),
            center_z=round((min_z + max_z) / 2.0, 3),
        )


def check_formation_geofence(
    points: Sequence[dict | tuple | list],
    limits: SafetyLimits = DEFAULT_LIMITS,
    min_separation: float | None = None,
) -> list[str]:
    """Check whether a formation clears the geofence cylinder and altitude bounds.

    Returns:
        List of human-readable error descriptions. Empty if safe.
    """
    errors: list[str] = []
    bounds = FormationBounds.from_points(points)

    if bounds.max_radius_m > limits.geofence_radius_m:
        errors.append(
            f"Formation exceeds geofence radius: max distance {bounds.max_radius_m:.2f}m "
            f"> limit {limits.geofence_radius_m:.2f}m"
        )

    if bounds.max_altitude_m > limits.max_altitude_m:
        errors.append(
            f"Formation exceeds altitude ceiling: max altitude {bounds.max_altitude_m:.2f}m "
            f"> limit {limits.max_altitude_m:.2f}m"
        )

    if bounds.min_altitude_m < 0.0 or bounds.max_z > 0.05:
        errors.append(
            f"Formation penetrates ground plane: lowest point at {bounds.min_altitude_m:.2f}m "
            f"(z = {bounds.max_z:.2f}m > 0.0m)"
        )

    return errors


def fit_formation_to_geofence(
    points: list[dict],
    limits: SafetyLimits = DEFAULT_LIMITS,
    min_separation: float | None = None,
    allow_reposition: bool = True,
    allow_scale: bool = True,
) -> list[dict]:
    """Ensure a formation fits within the geofence without violating separation.

    Strategy:
      1. If points already fit, return unchanged.
      2. If repositioning towards origin (0, 0) allows points to fit, center them.
      3. If scaling is allowed, scale down to fit with safety margin ONLY if
         inter-point distances remain >= min_separation.
      4. If the formation cannot fit without violating minimum separation,
         raise ValueError. Never silently compress below min_separation,
         never silently clip trajectories, and never delete drones.

    Raises:
        ValueError: If formation cannot safely fit within the geofence.
    """
    if not points:
        return points

    threshold = limits.min_separation_m if min_separation is None else min_separation
    bounds = FormationBounds.from_points(points)

    # 1. Fits already
    if bounds.max_radius_m <= limits.geofence_radius_m and bounds.max_altitude_m <= limits.max_altitude_m and bounds.min_altitude_m >= 0.0:
        return points

    pts = [{"x": p["x"], "y": p["y"], "z": p["z"]} for p in points]

    # 2. Try repositioning towards origin (0, 0) in XY
    if allow_reposition and (bounds.center_x != 0.0 or bounds.center_y != 0.0):
        # Shift center towards origin
        shifted = [
            {"x": p["x"] - bounds.center_x, "y": p["y"] - bounds.center_y, "z": p["z"]}
            for p in pts
        ]
        s_bounds = FormationBounds.from_points(shifted)
        if s_bounds.max_radius_m <= limits.geofence_radius_m:
            return shifted
        pts = shifted
        bounds = s_bounds

    # 3. Check scaling
    max_safe_r = limits.geofence_radius_m - 1.0  # 1.0m margin
    if bounds.max_radius_m > max_safe_r:
        if not allow_scale:
            raise ValueError(
                f"Formation radius ({bounds.max_radius_m:.2f}m) exceeds geofence radius "
                f"({limits.geofence_radius_m:.2f}m) and scaling is disabled."
            )

        scale_factor = max_safe_r / bounds.max_radius_m
        # Measure current minimum pairwise distance
        current_min_d = float("inf")
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                d = math.hypot(
                    math.hypot(pts[i]["x"] - pts[j]["x"], pts[i]["y"] - pts[j]["y"]),
                    pts[i]["z"] - pts[j]["z"],
                )
                if d < current_min_d:
                    current_min_d = d

        projected_min_d = current_min_d * scale_factor
        if projected_min_d < threshold:
            raise ValueError(
                f"Formation cannot fit inside geofence radius {limits.geofence_radius_m:.1f}m: "
                f"scaling by {scale_factor:.2f} to fit would compress drone separation to "
                f"{projected_min_d:.2f}m, below the {threshold:.2f}m minimum separation limit. "
                f"Reduce fleet size or expand geofence."
            )

        # Apply valid scaling
        pts = [
            {"x": p["x"] * scale_factor, "y": p["y"] * scale_factor, "z": p["z"]}
            for p in pts
        ]

    return pts
