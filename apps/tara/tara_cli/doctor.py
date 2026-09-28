"""`tara doctor` — checks the environment before `tara up` wastes your time on it.

Every check here corresponds to a failure mode that otherwise surfaces deep
inside a service's log output, prefixed and interleaved with four other
services, which is a bad way to discover a missing `npm install`.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import TaraConfig


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def run_checks(config: TaraConfig) -> list[CheckResult]:
    results = []

    py = config.venv_python
    results.append(
        CheckResult(
            "Python virtualenv",
            py.exists(),
            str(py) if py.exists() else f"not found at {py} — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt",
        )
    )

    for module, label in (("fastapi", "fastapi"), ("mavsdk", "mavsdk"), ("scipy", "scipy"), ("numpy", "numpy")):
        results.append(_check_python_module(py, module, label))

    npm = shutil.which("npm")
    results.append(CheckResult("npm on PATH", npm is not None, npm or "not found — install Node.js"))

    for pkg_dir, label in (
        (config.repo_root / "packages" / "tara-web", "tara-web node_modules"),
        (config.repo_root / "packages" / "tara-studio-web", "tara-studio-web node_modules"),
    ):
        installed = (pkg_dir / "node_modules").exists()
        results.append(
            CheckResult(
                label,
                installed,
                "installed" if installed else f"missing — run: (cd {pkg_dir} && npm install)",
            )
        )

    if config.backend == "px4":
        autopilot = Path(config.px4_autopilot_dir)
        px4_binary = autopilot / "build" / "px4_sitl_default" / "bin" / "px4"
        results.append(
            CheckResult(
                "PX4-Autopilot build",
                px4_binary.exists(),
                str(px4_binary) if px4_binary.exists() else f"not found at {px4_binary} — build PX4 first, or set PX4_AUTOPILOT_DIR (or [px4].autopilot_dir in tara.toml)",
            )
        )
        # `tara up` prepends /usr/bin to PATH for the fleet service specifically
        # (see services.py) to route around this, but check the ambient PATH
        # too: if the real Gazebo isn't installed at /usr/bin at all, that
        # workaround has nothing to fall back on and PX4 will hang waiting for
        # a world that never comes up.
        gz = shutil.which("gz")
        real_gz = Path("/usr/bin/gz")
        if gz and "ros" in gz.lower():
            results.append(
                CheckResult(
                    "Gazebo (gz) on PATH",
                    real_gz.exists(),
                    f"PATH resolves to the ROS-vendored {gz}, not the real Gazebo Sim — "
                    "`tara up` works around this by prepending /usr/bin, but only if "
                    f"{real_gz} exists" + ("" if real_gz.exists() else " (it does not)"),
                )
            )
        else:
            results.append(CheckResult("Gazebo (gz) on PATH", gz is not None, gz or "not found"))

    if config.rviz:
        rviz2 = shutil.which("rviz2")
        results.append(CheckResult("rviz2 on PATH", rviz2 is not None, rviz2 or "not found — source /opt/ros/jazzy/setup.bash"))

    return results


def _check_python_module(py: Path, module: str, label: str) -> CheckResult:
    if not py.exists():
        return CheckResult(f"Python: {label}", False, "venv missing, see above")
    proc = subprocess.run(
        [str(py), "-c", f"import {module}"], capture_output=True, text=True, check=False
    )
    ok = proc.returncode == 0
    return CheckResult(f"Python: {label}", ok, "importable" if ok else proc.stderr.strip().splitlines()[-1])
