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

#: The only PX4 tag this project has verified end-to-end (issue #177). Newer
#: PX4 `main` (as of 2026-10) moved gz_env.sh out of build/.../rootfs/ and its
#: barometer stream times out against Gazebo Sim 8.15 — see sim/px4/README.md.
SUPPORTED_PX4_TAG = "v1.16.2"

#: Where a committed copy of the custom show world would live, if one exists.
#: `launch_fleet.sh` installs it into the PX4 checkout before spawning; absent
#: here means the one genuinely missing piece of issue #177 (lost in the
#: monorepo move — see CHANGELOG 1.0.0) still hasn't been supplied.
WORLD_FILE_RELATIVE_PATH = ("sim", "px4", "worlds", "drone_show_field.sdf")


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
        results.append(_check_world_file(config.repo_root))
        if autopilot.is_dir():
            results.append(_check_px4_version(autopilot))
            results.append(_check_gz_env(autopilot))
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


def _check_world_file(repo_root: Path) -> CheckResult:
    world = repo_root.joinpath(*WORLD_FILE_RELATIVE_PATH)
    return CheckResult(
        "drone_show_field.sdf world",
        world.exists(),
        str(world) if world.exists() else (
            f"missing at {world} — launch_fleet.sh refuses to start without it rather "
            "than silently falling back to a default Gazebo world with no launch pads "
            "or geofence ring (issue #177)"
        ),
    )


def _check_px4_version(autopilot_dir: Path) -> CheckResult:
    if not (autopilot_dir / ".git").exists():
        return CheckResult(
            "PX4 version",
            True,
            f"not a git checkout, cannot verify — only {SUPPORTED_PX4_TAG} is confirmed working (see sim/px4/README.md)",
        )
    proc = subprocess.run(
        ["git", "-C", str(autopilot_dir), "describe", "--tags", "--exact-match"],
        capture_output=True, text=True, check=False,
    )
    tag = proc.stdout.strip()
    if proc.returncode == 0 and tag == SUPPORTED_PX4_TAG:
        return CheckResult("PX4 version", True, tag)
    detail = tag or "untagged or detached checkout"
    return CheckResult(
        "PX4 version",
        False,
        f"{detail} — only {SUPPORTED_PX4_TAG} is confirmed working (newer PX4 main has a "
        f"barometer/Gazebo Sim 8.15 timeout; see sim/px4/README.md). Fix: "
        f"git -C {autopilot_dir} checkout {SUPPORTED_PX4_TAG}",
    )


def _check_gz_env(autopilot_dir: Path) -> CheckResult:
    # PX4 main moved this file in 2026-10; support both locations so the
    # pinned v1.16.2 (old layout) and a newer checkout both resolve correctly.
    old = autopilot_dir / "build" / "px4_sitl_default" / "rootfs" / "gz_env.sh"
    new = autopilot_dir / "build" / "px4_sitl_default" / "gz_env.sh"
    found = old if old.exists() else new if new.exists() else None
    return CheckResult(
        "PX4 gz_env.sh",
        found is not None,
        str(found) if found else f"not found at {old} or {new} — build PX4 SITL first: (cd {autopilot_dir} && make px4_sitl gz_x500)",
    )


def _check_python_module(py: Path, module: str, label: str) -> CheckResult:
    if not py.exists():
        return CheckResult(f"Python: {label}", False, "venv missing, see above")
    proc = subprocess.run(
        [str(py), "-c", f"import {module}"], capture_output=True, text=True, check=False
    )
    ok = proc.returncode == 0
    return CheckResult(f"Python: {label}", ok, "importable" if ok else proc.stderr.strip().splitlines()[-1])
