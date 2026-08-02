"""Loads tara.toml.

Every value here is also overridable by a CLI flag; the file only supplies
defaults so `tara up` can be run with no arguments at all.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TaraConfig:
    repo_root: Path
    num_drones: int = 3
    backend: str = "px4"
    px4_autopilot_dir: str = "/home/harsh-pandhe/PX4-Autopilot"
    gcs_backend_port: int = 8090
    gcs_web_port: int = 5173
    studio_backend_port: int = 8001
    studio_web_port: int = 5174
    rviz: bool = False

    @property
    def venv_python(self) -> Path:
        return self.repo_root / ".venv" / "bin" / "python"


def find_repo_root(start: Path | None = None) -> Path:
    """Walk upward from ``start`` (default: this file) to find tara.toml."""
    here = (start or Path(__file__)).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "tara.toml").exists():
            return candidate
    raise FileNotFoundError(
        "Could not find tara.toml above "
        f"{start or Path(__file__)} — run `tara` from within the repo."
    )


def load_config(repo_root: Path | None = None) -> TaraConfig:
    root = repo_root or find_repo_root()
    data = tomllib.loads((root / "tara.toml").read_text())

    fleet = data.get("fleet", {})
    px4 = data.get("px4", {})
    ports = data.get("ports", {})
    services = data.get("services", {})

    return TaraConfig(
        repo_root=root,
        num_drones=fleet.get("num_drones", 3),
        backend=fleet.get("backend", "px4"),
        px4_autopilot_dir=px4.get("autopilot_dir", "/home/harsh-pandhe/PX4-Autopilot"),
        gcs_backend_port=ports.get("gcs_backend", 8090),
        gcs_web_port=ports.get("gcs_web", 5173),
        studio_backend_port=ports.get("studio_backend", 8001),
        studio_web_port=ports.get("studio_web", 5174),
        rviz=services.get("rviz", False),
    )
