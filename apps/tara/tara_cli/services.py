"""Builds the concrete :class:`~tara_cli.process.Service` list for one `tara up` run."""

from __future__ import annotations

import os

from .config import TaraConfig
from .process import Service


class UnsupportedBackendError(RuntimeError):
    pass


def build_services(
    config: TaraConfig,
    *,
    num_drones: int,
    backend: str,
    headless: bool,
    rviz: bool,
) -> list[Service]:
    """Return the ordered list of services for one `tara up` invocation.

    Args:
        config: Loaded tara.toml, providing ports and paths.
        num_drones: Fleet size for this run.
        backend: "px4" (real SITL) or "sim" (vectorized simulator).
        headless: Run PX4/Gazebo without the GUI (ignored for the sim backend).
        rviz: Also launch the ROS2 TF bridge + RViz.

    Returns:
        Services in launch order. The caller is expected to add them to a
        :class:`~tara_cli.process.Supervisor` in this order, since later
        entries depend on earlier ones being up.

    Raises:
        UnsupportedBackendError: If ``backend`` is neither "px4" nor "sim", or
            ``rviz`` is requested with ``backend="sim"`` (there is no real
            Gazebo TF to visualize without PX4).
    """
    root = config.repo_root
    py = str(config.venv_python)
    services: list[Service] = []

    if backend == "px4":
        fleet_cmd = ["bash", str(root / "sim" / "px4" / "launch_fleet.sh"), "-n", str(num_drones)]
        if headless:
            fleet_cmd.append("-h")
        services.append(
            Service(
                name="fleet",
                command=fleet_cmd,
                cwd=str(root),
                env={
                    "PX4_AUTOPILOT_DIR": config.px4_autopilot_dir,
                    # PX4's own startup script shells out to `gz`. On a machine
                    # with ROS 2 sourced, ROS vendors its own `gz` CLI ahead of
                    # the real Gazebo Sim install on PATH, and PX4's world never
                    # comes up under it (silent "Timed out waiting for Gazebo
                    # world"). /usr/bin first restores the real binary.
                    "PATH": f"/usr/bin:{os.environ.get('PATH', '')}",
                },
            )
        )
    elif backend == "sim":
        # No PX4, no Gazebo, no PX4_AUTOPILOT_DIR dependency at all — gcs-api
        # (below) drives tara_sim's vectorized fleet directly in-process.
        pass
    else:
        raise UnsupportedBackendError(f"unknown backend '{backend}', expected 'px4' or 'sim'")

    gcs_backend_url = f"http://localhost:{config.gcs_backend_port}"
    studio_backend_url = f"http://localhost:{config.studio_backend_port}"

    services.append(
        Service(
            name="gcs-api",
            command=[
                py, "-m", "uvicorn", "tara_gcs.main:app",
                "--host", "0.0.0.0", "--port", str(config.gcs_backend_port),
            ],
            cwd=str(root),
            env={
                "PYTHONPATH": (
                    f"{root / 'packages' / 'tara-gcs'}:"
                    f"{root / 'packages' / 'tara-core'}:"
                    f"{root / 'packages' / 'tara-sim'}"
                ),
                "GCS_PORT": str(config.gcs_backend_port),
                "NUM_DRONES": str(num_drones),
                "VEHICLE_BACKEND": backend,
            },
        )
    )

    services.append(
        Service(
            name="studio-api",
            command=[
                py, "-m", "uvicorn", "tara_studio.server:app",
                "--host", "127.0.0.1", "--port", str(config.studio_backend_port),
            ],
            cwd=str(root),
            env={
                "PYTHONPATH": f"{root / 'packages' / 'tara-studio'}:{root / 'packages' / 'tara-core'}",
                "STUDIO_PORT": str(config.studio_backend_port),
            },
        )
    )

    services.append(
        Service(
            name="gcs-web",
            command=["npm", "run", "dev", "--", "--port", str(config.gcs_web_port)],
            cwd=str(root / "packages" / "tara-web"),
            env={
                "VITE_API_BASE": gcs_backend_url,
                "VITE_WS_URL": f"ws://localhost:{config.gcs_backend_port}/ws/telemetry",
            },
        )
    )

    services.append(
        Service(
            name="studio-web",
            command=["npm", "run", "dev", "--", "--port", str(config.studio_web_port)],
            cwd=str(root / "packages" / "tara-studio-web"),
            env={
                "VITE_API_BASE": studio_backend_url,
                "VITE_GCS_BASE": gcs_backend_url,
                "VITE_DISABLE_AUTH": "true",
            },
        )
    )

    if rviz:
        if backend != "px4":
            raise UnsupportedBackendError("rviz requires backend = \"px4\" (it visualizes real Gazebo TF)")
        services.append(
            Service(
                name="rviz",
                command=["bash", str(root / "sim" / "ros" / "launch_rviz.sh")],
                cwd=str(root),
            )
        )

    return services
