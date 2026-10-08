"""Issue #55: the repo's command-line show generators write files the GCS accepts.

``tools/generate_trajectory.py`` and ``packages/tara-studio/scripts/generate_cli.py``
are scripts, not packages, so they are loaded from their paths.
"""

import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tara_core import SHOW_FORMAT_VERSION, Show
from tara_gcs.trajectory_parser import parse_trajectory_bytes

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return _load("generate_trajectory", REPO_ROOT / "tools" / "generate_trajectory.py")


def _csv_rows_as_show(path: Path) -> dict:
    """The CSV exactly as written, as a canonical show: no GCS normalisation."""
    drones: dict = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            wp = {k: float(v) for k, v in row.items() if k != "drone_id"}
            drones.setdefault(int(row["drone_id"]), []).append(wp)
    return {"version": SHOW_FORMAT_VERSION, "drones": [{"id": i, "waypoints": w} for i, w in drones.items()]}


@pytest.mark.parametrize("shape", ["circle", "helix"])
def test_generate_trajectory_never_writes_yaw_360(tool, shape):
    # 7 drones at 7 Hz puts a heading in [359.95, 360): wrapping before
    # rounding used to write it out as 360.0, outside the schema's [0, 360).
    drones = getattr(tool, f"generate_{shape}_trajectory")(7, 30.0, 7.0)
    show = {"version": SHOW_FORMAT_VERSION, "drones": [{"id": i, "waypoints": w} for i, w in drones.items()]}
    Show.validate_and_load(show)


@pytest.mark.parametrize("shape", ["circle", "helix"])
@pytest.mark.parametrize("ext", [".json", ".csv"])
def test_generate_trajectory_output_uploads_to_the_gcs(tool, tmp_path, monkeypatch, shape, ext):
    # Two drones: the tool's default three-drone layout comes within 1.32 m,
    # which the GCS's 1.5 m collision gate refuses whatever the file format.
    out = tmp_path / f"show{ext}"
    monkeypatch.setattr(sys, "argv", ["generate_trajectory.py", "-n", "2", "-s", shape, "-o", str(out)])
    tool.main()

    if ext == ".json":
        Show.validate_and_load({"version": SHOW_FORMAT_VERSION, **json.loads(out.read_text())})
    else:
        Show.validate_and_load(_csv_rows_as_show(out))
    staged = parse_trajectory_bytes(out.read_bytes(), out.name)
    assert sorted(staged) == [0, 1]


def test_studio_generate_cli_output_uploads_to_the_gcs(tmp_path, monkeypatch):
    cli = _load("generate_cli", REPO_ROOT / "packages" / "tara-studio" / "scripts" / "generate_cli.py")
    out = tmp_path / "show.csv"
    monkeypatch.setattr(sys, "argv", ["generate_cli.py", "-o", str(out)])
    cli.main()

    Show.validate_and_load(_csv_rows_as_show(out))
    staged = parse_trajectory_bytes(out.read_bytes(), out.name)
    assert len(staged) == 4  # the CLI's default fleet
