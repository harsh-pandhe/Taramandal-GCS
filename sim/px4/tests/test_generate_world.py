"""Tests for sim/px4/generate_world.py (issue #177).

The thing worth proving isn't "valid XML" alone (trivial) - it's that the
generated pad positions are mechanically identical to what
`tara_gcs.drone_manager._pad_offset` and `launch_fleet.sh` actually use, not
a hand-copied approximation of them that could silently drift.
"""

from __future__ import annotations

import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import generate_world  # noqa: E402

from tara_core import DEFAULT_LIMITS  # noqa: E402
from tara_core.layout import launch_grid, MAX_FLEET_SIZE  # noqa: E402


def test_pad_count_matches_the_geofence_column_cap():
    assert generate_world.PAD_COUNT == math.floor(
        DEFAULT_LIMITS.geofence_radius_m / DEFAULT_LIMITS.pad_spacing_m
    )


def test_pad_positions_are_identical_to_the_gcs_fixed_per_drone_pad():
    """Same function, same arguments `_pad_offset` itself calls - not a
    reimplementation that happens to agree today."""
    expected = launch_grid(MAX_FLEET_SIZE)[: generate_world.PAD_COUNT]
    actual = generate_world._pad_positions()
    assert actual == expected


def test_pad_positions_are_a_pure_y_axis_line_matching_launch_fleet_sh():
    """launch_fleet.sh spawns every instance at x=0, Y_OFFSET = PAD_SPACING_Y
    * i. If this ever stopped being true, the generated world's pads would
    silently stop matching where PX4 SITL actually spawns drones."""
    for i, (x, y) in enumerate(generate_world._pad_positions()):
        assert x == 0.0
        assert y == DEFAULT_LIMITS.pad_spacing_m * i


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
