"""Tests for sim/px4/generate_world.py (issue #177).

The thing worth proving isn't "valid XML" alone (trivial) - it's that the
generated pad positions are mechanically identical to what
`tara_gcs.drone_manager._pad_offset` and `launch_fleet.sh` actually use, not
a hand-copied approximation of them that could silently drift.
"""

from __future__ import annotations

import math
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import generate_world  # noqa: E402

from tara_core import DEFAULT_LIMITS  # noqa: E402
from tara_core.layout import launch_grid, MAX_FLEET_SIZE  # noqa: E402


def test_pad_count_matches_the_geofence_column_cap():
    assert generate_world.PAD_COUNT == math.floor(
        DEFAULT_LIMITS.geofence_radius_m / DEFAULT_LIMITS.pad_spacing_m
    )


def test_fleet_frame_pads_are_identical_to_the_gcs_fixed_per_drone_pad():
    """Same function, same arguments `_pad_offset` itself calls - not a
    reimplementation that happens to agree today."""
    expected = launch_grid(MAX_FLEET_SIZE)[: generate_world.PAD_COUNT]
    actual = generate_world._pad_positions()
    assert actual == expected


def test_fleet_frame_pads_are_a_pure_y_axis_line():
    for i, (x, y) in enumerate(generate_world._pad_positions()):
        assert x == 0.0
        assert y == DEFAULT_LIMITS.pad_spacing_m * i


def test_fleet_y_is_gazebo_east_not_gazebo_north():
    """Issue #172 F1. The GCS fleet frame is NED (x north, y east); Gazebo's
    world frame is ENU (x east, y north). A pad at fleet (x, y) therefore sits
    at Gazebo (east=y, north=x). Copying (x, y) across put drone i 2.2*i m
    NORTH in Gazebo while the GCS believed it was EAST - every proximity and
    geofence check ran on positions off by (-2.2*i, +2.2*i) m."""
    assert generate_world.fleet_to_gazebo(3.0, 5.0) == (5.0, 3.0)  # (east, north)
    assert generate_world.fleet_to_gazebo(0.0, 2.2) == (2.2, 0.0)


def test_world_pads_sit_where_the_gcs_believes_each_drone_is():
    """Read the pose back out of the generated SDF, as Gazebo will, and map it
    to the fleet frame the way the GCS does (north=gz y, east=gz x)."""
    root = ET.fromstring(generate_world.generate())
    pads = {m.get("name"): m for m in root.find("world").findall("model")}
    for i, (fleet_x, fleet_y) in enumerate(generate_world._pad_positions()):
        gz_x, gz_y, _z, *_ = (float(v) for v in pads[f"pad_{i}"].find("pose").text.split())
        assert (gz_y, gz_x) == (pytest.approx(fleet_x, abs=1e-3), pytest.approx(fleet_y, abs=1e-3))


def _spawn_poses() -> tuple[str, str]:
    """(host pose, follower pose template) as written in launch_fleet.sh."""
    text = (Path(generate_world.__file__).parent / "launch_fleet.sh").read_text()
    host, follower = re.findall(r'PX4_GZ_MODEL_POSE="([^"]+)"', text)
    return host, follower


def test_launch_fleet_spawns_each_drone_on_the_pad_the_gcs_assumes():
    """launch_fleet.sh must put drone i at the Gazebo position of its fleet-frame
    pad. Substitutes the script's own offset variable into its own pose string
    rather than restating the axis here."""
    host, follower = _spawn_poses()
    host_east, host_north, *_ = (float(v) for v in host.split(","))
    assert (host_east, host_north) == generate_world.fleet_to_gazebo(*generate_world._pad_positions()[0])

    (var,) = re.findall(r"\$\{(\w+)\}", follower)
    for i, pad in enumerate(generate_world._pad_positions()):
        east, north, *_ = (float(v) for v in follower.replace("${" + var + "}", repr(DEFAULT_LIMITS.pad_spacing_m * i)).split(","))
        assert (east, north) == pytest.approx(generate_world.fleet_to_gazebo(*pad))


def test_output_is_well_formed_xml():
    doc = generate_world.generate()
    root = ET.fromstring(doc)  # raises on malformed XML
    assert root.tag == "sdf"
    assert root.get("version") == "1.9"


def test_world_has_the_expected_models():
    doc = generate_world.generate()
    root = ET.fromstring(doc)
    world = root.find("world")
    assert world.get("name") == "drone_show_field"
    names = [m.get("name") for m in world.findall("model")]
    assert "ground_plane" in names
    for i in range(generate_world.PAD_COUNT):
        assert f"pad_{i}" in names
    assert sum(1 for n in names if n.startswith("geofence_marker_")) == generate_world.GEOFENCE_SEGMENTS


def test_geofence_markers_sit_exactly_on_the_real_geofence_radius():
    doc = generate_world.generate()
    root = ET.fromstring(doc)
    world = root.find("world")
    r = DEFAULT_LIMITS.geofence_radius_m
    found = 0
    for model in world.findall("model"):
        if not model.get("name", "").startswith("geofence_marker_"):
            continue
        x, y, _z, *_ = (float(v) for v in model.find("pose").text.split())
        # abs_tol, not rel_tol: positions are formatted to 3 decimal places
        # (mm precision) in the SDF output, which is the real source of any
        # difference here, not a geometry error.
        assert math.isclose(math.hypot(x, y), r, abs_tol=1e-3)
        found += 1
    assert found == generate_world.GEOFENCE_SEGMENTS


def test_pads_outside_the_column_cap_would_be_refused_not_silently_wrong():
    """A future spacing/radius change that breaks the line-property
    assumption must fail loudly (see generate_world._pad_positions), not
    silently emit a world whose pads no longer match real spawn positions."""
    import pytest

    original = generate_world.PAD_COUNT
    try:
        generate_world.PAD_COUNT = len(launch_grid(MAX_FLEET_SIZE))  # force a wrap
        with pytest.raises(RuntimeError, match="no longer a Y-axis line"):
            generate_world._pad_positions()
    finally:
        generate_world.PAD_COUNT = original


def _run_world_install_block(script_dir: Path, px4_dir: Path) -> None:
    """Run launch_fleet.sh's own world-install block, and nothing else.

    The rest of the script kills running PX4/Gazebo processes, so it is not
    safe to execute under test; the block is cut out between its two comment
    anchors and run with the variables the script would have set.
    """
    import subprocess

    text = (Path(generate_world.__file__).parent / "launch_fleet.sh").read_text()
    block = text[text.index("WORLD_DEST_DIR="): text.index("# Source Gazebo environment")]
    subprocess.run(
        ["bash", "-c", block],
        check=True,
        env={"PATH": "/usr/bin:/bin", "SCRIPT_DIR": str(script_dir), "PX4_AUTOPILOT_DIR": str(px4_dir)},
    )


def test_launch_fleet_replaces_a_stale_installed_world(tmp_path):
    """Issue #172 F1: the world is generated, so a copy installed by an earlier
    run (pads on the old north axis) must not survive a fix to the generator."""
    script_dir = tmp_path / "sim"
    (script_dir / "worlds").mkdir(parents=True)
    (script_dir / "worlds" / "drone_show_field.sdf").write_text("new world")
    installed = tmp_path / "px4" / "Tools" / "simulation" / "gz" / "worlds" / "drone_show_field.sdf"
    installed.parent.mkdir(parents=True)
    installed.write_text("stale world")

    _run_world_install_block(script_dir, tmp_path / "px4")

    assert installed.read_text() == "new world"


def test_launch_fleet_installs_the_world_when_none_is_installed(tmp_path):
    script_dir = tmp_path / "sim"
    (script_dir / "worlds").mkdir(parents=True)
    (script_dir / "worlds" / "drone_show_field.sdf").write_text("new world")
    (tmp_path / "px4").mkdir()

    _run_world_install_block(script_dir, tmp_path / "px4")

    installed = tmp_path / "px4" / "Tools" / "simulation" / "gz" / "worlds" / "drone_show_field.sdf"
    assert installed.read_text() == "new world"
