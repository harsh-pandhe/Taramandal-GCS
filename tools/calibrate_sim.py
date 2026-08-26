#!/usr/bin/env python3
"""Calibrates `tara_sim`'s fidelity against real PX4 SITL — a gate, not a formality.

The 100-drone claim in the finishing plan rests entirely on `tara_sim.FleetState`
being an honest stand-in for real PX4 offboard behaviour. This script is how that
claim earns the right to be believed: fly the identical short reference
trajectory through both backends at the same fleet size, record both position
traces, and compute the per-drone RMS position error between them.

Budget: RMS error must stay well under the 0.5m margin between
`SafetyLimits.design_separation_m` (2.0) and `min_separation_m` (1.5) — i.e.
simulator error must not be able to eat the safety margin a certified show
relies on. If it doesn't clear that bound, the honest next step is raising sim
fidelity (attitude dynamics, a rate controller), not shipping the 100-drone
number anyway.

Usage:
    # 1. Start the real fleet separately first (this script does not manage it):
    PATH="/usr/bin:$PATH" bash sim/px4/launch_fleet.sh -n 8 -h

    # 2. Then, from the repo root:
    PYTHONPATH=packages/tara-gcs:packages/tara-core:packages/tara-sim \
        .venv/bin/python tools/calibrate_sim.py

STATUS (as of this writing): blocked on this dev machine, for a reason
unrelated to tara_sim. Two runs against a fresh 8-drone PX4 fleet both found
`health().is_global_position_ok` and `.is_home_position_ok` permanently False
after 20+ seconds of polling — this SITL world/build never achieves a GPS/home
lock, so every real drone's armable-wait times out and `_launch_one` returns
without arming. The resulting "calibration" numbers (RMS error growing
~linearly with drone index, 2m to 15m) are the signature of stationary
unarmed real drones being compared against a sim fleet that did fly — not a
real physics divergence, and not evidence tara_sim's fidelity is poor.

Setting `COM_ARM_WO_GPS=1` (the standard arm-without-GPS override) does not
unblock arming in this build; broader circuit-breaker/EKF param relaxation
attempts either had no effect or referenced params that don't exist in this
PX4 build at all. This needs someone with access to this PX4 build's actual
source/config (world file GPS/navsat sensor plugin, EKF2 aiding-source setup)
to resolve — see the "Fix real PX4 SITL never achieving GPS/home-position
lock" task. Until then, this script's PX4-side trace cannot be trusted; the
100-drone sim milestone stands on its own correctness (zero proximity/geofence
violations, `packages/tara-gcs/tests/test_sim_backend.py`), just without a
live real-fleet fidelity number to cite alongside it.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "tara-gcs"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "tara-core"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "tara-sim"))

from tara_core import altitude_layers  # noqa: E402
from tara_gcs.drone_manager import DroneManager, _pad_offset  # noqa: E402

N = 8
SAMPLE_HZ = 10.0
SHOW_DURATION_S = 12.0
RMS_BUDGET_M = 0.5  # design_separation_m (2.0) - min_separation_m (1.5)


def reference_trajectory(n: int) -> dict:
    """Climb, hold, a small lateral move, then hold again — enough to exercise
    both the position and velocity-feedforward setpoint paths without being a
    full show."""
    alts = altitude_layers(n)
    show = {}
    for i in range(n):
        x, y = _pad_offset(i)
        alt = alts[i]
        show[i] = [
            {"time": 0.0, "x": x, "y": y, "z": 0.0, "yaw": 0.0},
            {"time": 4.0, "x": x, "y": y, "z": -alt, "yaw": 0.0},
            {"time": 6.0, "x": x, "y": y, "z": -alt, "yaw": 0.0},
            {"time": 9.0, "x": x + 2.0, "y": y, "z": -alt, "yaw": 0.0},
            {"time": SHOW_DURATION_S, "x": x + 2.0, "y": y, "z": -alt, "yaw": 0.0},
        ]
    return show


async def record_trace(manager: DroneManager, n: int, duration: float, sample_hz: float) -> dict[int, list[tuple[float, float, float]]]:
    trace: dict[int, list[tuple[float, float, float]]] = {i: [] for i in range(n)}
    samples = int(duration * sample_hz)
    dt = 1.0 / sample_hz
    for _ in range(samples):
        for i in range(n):
            tel = manager.telemetry.get(i)
            if tel:
                trace[i].append((tel["local_x"], tel["local_y"], tel["local_z"]))
            else:
                trace[i].append((0.0, 0.0, 0.0))
        await asyncio.sleep(dt)
    return trace


async def fly_and_record(backend: str, n: int) -> dict[int, list[tuple[float, float, float]]]:
    ports = [14540 + i for i in range(n)]
    manager = DroneManager(ports=ports, backend=backend)
    try:
        await manager.connect_all()
        # Real PX4 SITL's EKF (GPS/accel bias convergence) can take well over
        # 15s to settle with several instances competing for CPU on one
        # machine — the internal armable-wait inside launch_sequence has its
        # own 15s timeout per drone, so if EKF isn't even close to converged
        # by the time that starts, drones get skipped ("did not become
        # armable") rather than actually flown, which corrupted an earlier
        # run of this script (RMS errors that grew ~linearly with drone
        # index — the signature of unarmed real drones sitting at the origin
        # while their sim counterparts flew the full trajectory).
        await asyncio.sleep(3.0 if backend == "sim" else 25.0)
        await manager.launch_sequence()
        # One retry pass for any drone that missed the armable window the
        # first time (launch_sequence is explicitly idempotent/safe to re-call).
        if backend == "px4":
            await asyncio.sleep(5.0)
            await manager.launch_sequence()
        await asyncio.sleep(2.0 + 1.0 * n)  # bounded-batch stagger, matches launch_sequence's own timing
        show = reference_trajectory(n)
        manager.start_trajectory(show, mode="LOCAL")
        trace = await record_trace(manager, n, SHOW_DURATION_S + 1.0, SAMPLE_HZ)
        return trace
    finally:
        await manager.shutdown()


def rms_error(a: list[tuple[float, float, float]], b: list[tuple[float, float, float]]) -> float:
    n = min(len(a), len(b))
    if n == 0:
        return float("inf")
    total = 0.0
    for i in range(n):
        dx = a[i][0] - b[i][0]
        dy = a[i][1] - b[i][1]
        dz = a[i][2] - b[i][2]
        total += dx * dx + dy * dy + dz * dz
    return (total / n) ** 0.5


async def main() -> int:
    print(f"=== Calibrating tara_sim against real PX4 SITL, N={N} ===")
    print("Recording SIM trace...")
    sim_trace = await fly_and_record("sim", N)
    print("Recording PX4 trace (assumes sim/px4/launch_fleet.sh -n 8 already running)...")
    px4_trace = await fly_and_record("px4", N)

    results = {}
    worst = 0.0
    for i in range(N):
        err = rms_error(sim_trace[i], px4_trace[i])
        results[i] = round(err, 4)
        worst = max(worst, err)
        print(f"  Drone {i}: RMS error = {err:.3f} m")

    passed = worst < RMS_BUDGET_M
    print(f"\nWorst-case RMS error: {worst:.3f} m (budget: < {RMS_BUDGET_M} m)")
    print("PASSED" if passed else "FAILED — sim fidelity is not sufficient; raise fidelity before trusting N=100")

    out = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "n_drones": N,
        "rms_budget_m": RMS_BUDGET_M,
        "per_drone_rms_m": results,
        "worst_case_rms_m": round(worst, 4),
        "passed": passed,
    }
    out_path = Path(__file__).resolve().parent / "calibration_results.json"
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\nResults written to {out_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
