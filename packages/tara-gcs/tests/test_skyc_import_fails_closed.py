"""A .skyc the importer cannot read is refused, with the reason, never imported as a drone that does not fly (issue #86).

The importer reads ``drone.trajectory.points``. A real Skybrush file may nest it differently (the issue's example is
``drone.settings.trajectory``); without a real file nothing here guesses where else it might be. What these tests hold:
every drone entry the reader cannot get a waypoint from is an explicit, specific error, and one unreadable drone refuses
the whole show. The supported shape is unchanged (test_show_contract_e2e.py round-trips it).
"""

import io
import json
import zipfile

import pytest

from tara_gcs.trajectory_parser import parse_trajectory_bytes

POINTS = [[0, [0, 0, 0], []], [5, [0, 0, -5], []], [10, [0, 0, 0], []]]


def _skyc(drones: list) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("show.json", json.dumps({"swarm": {"drones": drones}}))
    return buf.getvalue()


def test_the_supported_shape_is_still_read():
    shown = parse_trajectory_bytes(_skyc([{"trajectory": {"points": POINTS}}]), "show.skyc")
    assert [w["time"] for w in shown[0]] == [0.0, 5.0, 10.0]


def test_the_nested_settings_shape_from_the_issue_is_refused_and_says_what_was_found():
    with pytest.raises(ValueError) as exc:
        parse_trajectory_bytes(_skyc([{"settings": {"trajectory": {"points": POINTS}}}]), "show.skyc")
    text = str(exc.value)
    assert "drone 0 has no readable trajectory" in text
    assert "settings" in text  # the keys it did find
    assert "#86" in text


@pytest.mark.parametrize(
    "drone, reason",
    [
        ({"trajectory": {"points": []}}, "no readable [time, [x, y, z]] entry (0 point(s) found)"),
        ({"trajectory": {"points": ["junk", [1], [2, [1, 2]]]}}, "no readable [time, [x, y, z]] entry (3 point(s) found)"),
        ({"trajectory": []}, "no readable trajectory"),
        ({}, "no readable trajectory"),
        ("not an object", "no readable trajectory"),
    ],
    ids=["empty points", "malformed points", "trajectory not an object", "empty drone", "drone not an object"],
)
def test_a_drone_with_nothing_readable_is_refused_with_its_index(drone, reason):
    with pytest.raises(ValueError) as exc:
        parse_trajectory_bytes(_skyc([{"trajectory": {"points": POINTS}}, drone]), "show.skyc")
    assert "drone 1" in str(exc.value)
    assert reason in str(exc.value)


def test_one_unreadable_drone_refuses_the_whole_show_not_a_partial_one():
    with pytest.raises(ValueError):
        parse_trajectory_bytes(_skyc([{"trajectory": {"points": POINTS}}, {"settings": {}}]), "show.skyc")
