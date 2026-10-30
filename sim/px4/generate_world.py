#!/usr/bin/env python3
"""Generates sim/px4/worlds/drone_show_field.sdf (issue #177).

The original hand-authored world (CHANGELOG 1.0.0: custom-aligned launch
pads `pad_0`..`pad_4` + a visual red safety geofence ring) was lost in the
monorepo move and is not something this script tries to reconstruct - its
exact historical pad geometry is unknown, and guessing it would be worse
than no world at all (see sim/px4/README.md).

What this script generates instead is a *new*, mechanically correct world,
derived directly from this project's own authoritative constants rather than
invented:

- Pad positions: ``tara_core.layout.launch_grid(MAX_FLEET_SIZE)``, the same
  function ``tara_gcs.drone_manager._pad_offset`` indexes into for every real
  drone's fixed pad. For drone ids below the grid's column cap (13 at
  defaults), this is provably a plain Y-axis line - ``(0, i * pad_spacing_m)``
  - which is exactly what ``launch_fleet.sh`` already spawns PX4 SITL
  instances at (``PX4_GZ_MODEL_POSE="0,${Y_OFFSET},0.2,0,0,0"``). Generating
  N <= 13 pads keeps this script's output provably in agreement with both,
  rather than asserting it.
- Geofence ring: ``tara_core.safety.DEFAULT_LIMITS.geofence_radius_m`` (30m),
  drawn as a ring of small visual-only markers (SDF has no annulus
  primitive) - cosmetic rendering choice, not a guessed number.

Regenerate after any pad-spacing change (issue #167 is exactly this kind of
change) with:

    PYTHONPATH=packages/tara-core python3 sim/px4/generate_world.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages" / "tara-core"))

from tara_core import DEFAULT_LIMITS  # noqa: E402
from tara_core.layout import MAX_FLEET_SIZE, launch_grid  # noqa: E402

#: launch_grid(MAX_FLEET_SIZE)'s own column cap (floor(geofence_radius_m /
#: pad_spacing_m), 13 at current defaults) - the exact boundary below which
#: every drone id provably lands on a single Y-axis line (see this module's
#: docstring). Generating exactly this many, not an arbitrary round number,
#: keeps the "it's a line" property derived rather than asserted; it also
#: comfortably covers the real SITL range launch_fleet.sh is practical for.
PAD_COUNT = math.floor(DEFAULT_LIMITS.geofence_radius_m / DEFAULT_LIMITS.pad_spacing_m)
#: Number of point markers approximating the geofence ring's circumference.
GEOFENCE_SEGMENTS = 48
PAD_RADIUS_M = 0.4
PAD_HEIGHT_M = 0.02
OUT_PATH = Path(__file__).resolve().parent / "worlds" / "drone_show_field.sdf"


def _pad_positions() -> list[tuple[float, float]]:
    grid = launch_grid(MAX_FLEET_SIZE)[:PAD_COUNT]
    # Fail loudly, not silently, if a future layout.py change ever breaks the
    # straight-line property this world's pad placement assumes - a curved
    # grid here would silently diverge from launch_fleet.sh's own spawn math.
    for x, _y in grid:
        if abs(x) > 1e-9:
            raise RuntimeError(
                f"launch_grid(MAX_FLEET_SIZE) is no longer a Y-axis line within the first {PAD_COUNT} ids "
                f"(got x={x}) - sim/px4/generate_world.py's pad placement assumption no longer holds."
            )
    return grid


def _pad_model(name: str, x: float, y: float) -> str:
    return f"""    <model name="{name}">
      <static>true</static>
      <pose>{x:.3f} {y:.3f} {PAD_HEIGHT_M / 2:.3f} 0 0 0</pose>
      <link name="link">
        <visual name="visual">
          <geometry><cylinder><radius>{PAD_RADIUS_M}</radius><length>{PAD_HEIGHT_M}</length></cylinder></geometry>
          <material>
            <ambient>0.12 0.55 0.85 1</ambient>
            <diffuse>0.12 0.55 0.85 1</diffuse>
          </material>
        </visual>
      </link>
    </model>
"""


def _geofence_marker(name: str, x: float, y: float) -> str:
    return f"""    <model name="{name}">
      <static>true</static>
      <pose>{x:.3f} {y:.3f} 0.05 0 0 0</pose>
      <link name="link">
        <visual name="visual">
          <geometry><sphere><radius>0.15</radius></sphere></geometry>
          <material>
            <ambient>0.9 0.1 0.1 1</ambient>
            <diffuse>0.9 0.1 0.1 1</diffuse>
            <emissive>0.4 0 0 1</emissive>
          </material>
        </visual>
      </link>
    </model>
"""


def generate() -> str:
    pads = "".join(_pad_model(f"pad_{i}", x, y) for i, (x, y) in enumerate(_pad_positions()))

    r = DEFAULT_LIMITS.geofence_radius_m
    ring = "".join(
        _geofence_marker(
            f"geofence_marker_{i}",
            r * math.cos(2 * math.pi * i / GEOFENCE_SEGMENTS),
            r * math.sin(2 * math.pi * i / GEOFENCE_SEGMENTS),
        )
        for i in range(GEOFENCE_SEGMENTS)
    )

    # Base world skeleton (physics/gravity/atmosphere/scene/ground_plane/
    # light/spherical_coordinates) copied verbatim from PX4's own real,
    # upstream-maintained default world
    # (github.com/PX4/PX4-gazebo-models, worlds/default.sdf - the actual
    # file `make px4_sitl gz_x500` loads when no custom PX4_GZ_WORLD is
    # set), not invented: this is what every stock PX4 Gazebo world already
    # looks like, cross-checked against the real upstream source rather than
    # guessed from memory of SDF syntax. Only the <model name="pad_*">,
    # <model name="geofence_marker_*"> content below it is new.
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<sdf version="1.9">
  <!-- Generated by sim/px4/generate_world.py (issue #177) - do not hand-edit.
       Pad positions: tara_core.layout.launch_grid(MAX_FLEET_SIZE), the same
       function the GCS's real per-drone pad offset uses. Geofence ring
       radius: tara_core.safety.DEFAULT_LIMITS.geofence_radius_m. Base world
       skeleton copied from PX4/PX4-gazebo-models' own default.sdf. -->
  <world name="drone_show_field">
    <physics type="ode">
      <max_step_size>0.004</max_step_size>
      <real_time_factor>1.0</real_time_factor>
      <real_time_update_rate>250</real_time_update_rate>
    </physics>
    <gravity>0 0 -9.8</gravity>
    <magnetic_field>6e-06 2.3e-05 -4.2e-05</magnetic_field>
    <atmosphere type="adiabatic"/>
    <scene>
      <grid>false</grid>
      <ambient>0.4 0.4 0.4 1</ambient>
      <background>0.7 0.7 0.7 1</background>
      <shadows>true</shadows>
    </scene>
    <model name="ground_plane">
      <static>true</static>
      <link name="link">
        <collision name="collision">
          <pose>0 0 -5 0 0 0</pose>
          <geometry>
            <box>
              <size>100000 100000 10</size>
            </box>
          </geometry>
          <surface>
            <friction>
              <ode/>
            </friction>
            <bounce/>
            <contact/>
          </surface>
        </collision>
        <visual name="visual">
          <geometry>
            <plane>
              <normal>0 0 1</normal>
              <size>500 500</size>
            </plane>
          </geometry>
          <material>
            <ambient>0.8 0.8 0.8 1</ambient>
            <diffuse>0.8 0.8 0.8 1</diffuse>
            <specular>0.8 0.8 0.8 1</specular>
          </material>
        </visual>
        <pose>0 0 0 0 -0 0</pose>
        <inertial>
          <pose>0 0 0 0 -0 0</pose>
          <mass>1</mass>
          <inertia>
            <ixx>1</ixx>
            <ixy>0</ixy>
            <ixz>0</ixz>
            <iyy>1</iyy>
            <iyz>0</iyz>
            <izz>1</izz>
          </inertia>
        </inertial>
        <enable_wind>false</enable_wind>
      </link>
      <pose>0 0 0 0 -0 0</pose>
      <self_collide>false</self_collide>
    </model>
    <light name="sunUTC" type="directional">
      <pose>0 0 500 0 -0 0</pose>
      <cast_shadows>true</cast_shadows>
      <intensity>1</intensity>
      <direction>0.001 0.625 -0.78</direction>
      <diffuse>0.904 0.904 0.904 1</diffuse>
      <specular>0.271 0.271 0.271 1</specular>
      <attenuation>
        <range>2000</range>
        <linear>0</linear>
        <constant>1</constant>
        <quadratic>0</quadratic>
      </attenuation>
      <spot>
        <inner_angle>0</inner_angle>
        <outer_angle>0</outer_angle>
        <falloff>0</falloff>
      </spot>
    </light>
{pads}{ring}    <spherical_coordinates>
      <surface_model>EARTH_WGS84</surface_model>
      <world_frame_orientation>ENU</world_frame_orientation>
      <latitude_deg>47.397971057728974</latitude_deg>
      <longitude_deg> 8.546163739800146</longitude_deg>
      <elevation>488</elevation>
    </spherical_coordinates>
  </world>
</sdf>
"""


def main() -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(generate())
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
