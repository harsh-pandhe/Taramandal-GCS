#!/usr/bin/env python3
"""Flies a certified Diwali show through `tara_sim` (backend="sim") and measures it.

Takes the JSON that `tools/diwali_build.mjs` got back from the real Studio
`/api/export`, so what flies here is exactly the file a flight export produced,
not a rebuilt copy. It runs the same production path as
`tools/benchmark_fleet_scale.py` - `DroneManager` connect, mission upload,
`launch_sequence`, onboard `start_trajectory` - and reuses that script's tick,
task and resource probes, then adds what that benchmark cannot know because it
flies a synthetic show:

- **Flown separation**: the smallest pairwise distance between the simulated
  drones, from positions sampled at 5 Hz during the show (planned separation is
  already certified by the export; this is what the physics actually did).
- **Tracking error**: how far each drone was from its planned waypoint, before and
  after the fleet settles onto the plan (see the launch-layer note in `fly`). The plan
  is read against the mission's own UTC clock, so a sim that falls behind real time
  shows up here as lag rather than being hidden by comparing sim time to sim time.
- **Landing**: how many drones are back on the ground when the show ends.

Sim backend only - no PX4, no Gazebo.

Usage, from the repo root:

    python tools/diwali_sim_scale.py dist/diwali/diwali_100.json dist/diwali/diwali_500.json
    python tools/diwali_sim_scale.py --out dist/diwali/sim_scale.json dist/diwali/diwali_100.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
for _pkg in ("tara-core", "tara-gcs", "tara-sim"):
    sys.path.insert(0, str(_ROOT / "packages" / _pkg))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import benchmark_fleet_scale as bfs  # noqa: E402
from tara_gcs.drone_manager import DroneManager, _pad_offset  # noqa: E402

SAMPLE_HZ = 5.0
SETTLE_ERR_M = 5.0
SETTLE_WINDOW_S = 5.0


def load_show(path: Path) -> dict[int, list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {int(d["id"]): d["waypoints"] for d in data["drones"]}


def planned_at(times: np.ndarray, xyz: np.ndarray, t: float) -> np.ndarray:
    """Planned position of every drone at show time `t` (linear between waypoints)."""
    k = int(np.clip(np.searchsorted(times, t, side="right") - 1, 0, len(times) - 2))
    f = float(np.clip((t - times[k]) / (times[k + 1] - times[k]), 0.0, 1.0))
    return xyz[:, k] * (1 - f) + xyz[:, k + 1] * f


def min_pairwise(points: np.ndarray) -> float:
    """Smallest pairwise distance in an (n, 3) array without an n x n x 3 temporary."""
    sq = np.einsum("ij,ij->i", points, points)
    d2 = sq[:, None] + sq[None, :] - 2.0 * points @ points.T
    np.fill_diagonal(d2, np.inf)
    return float(np.sqrt(max(d2.min(), 0.0)))


async def fly(path: Path, raw_out: Path | None = None) -> dict:
    show = load_show(path)
    n = len(show)
    ids = sorted(show)
    times = np.array([w["time"] for w in show[ids[0]]])
    xyz = np.array([[[w["x"], w["y"], w["z"]] for w in show[i]] for i in ids])  # (n, T, 3)
    duration = float(times[-1])
    print(f"\n=== {path.name}: N={n}, planned {duration:.1f}s ===", flush=True)

    manager = DroneManager(ports=list(range(n)), backend="sim")
    tasks = bfs.TaskCounter(asyncio.get_running_loop())
    tasks.install()
    probe = bfs.ControlLoopProbe()
    result: dict = {"file": path.name, "n": n, "planned_duration_s": round(duration, 2)}
    phases: dict[str, float] = {}
    recorder = None
    pad = np.array([_pad_offset(i) for i in ids])  # world = pad origin + local, x/y only

    with bfs.ResourceSampler() as sampler:
        try:
            t0 = time.perf_counter()
            await manager.connect_all()
            phases["connect_s"] = time.perf_counter() - t0
            fs = manager._sim_fleet.state
            recorder = bfs.TickLatencyRecorder(fs, fs.config.dt, None)
            recorder.install()
            await asyncio.sleep(0.5)

            t0 = time.perf_counter()
            await manager.upload_show(show)
            phases["upload_s"] = time.perf_counter() - t0
            t0 = time.perf_counter()
            await manager.launch_sequence()
            phases["launch_s"] = time.perf_counter() - t0
            await asyncio.sleep(0.5)
            result["armed_in_physics"] = int(fs.armed.sum())

            recorder.reset()
            probe.start()
            tasks.reset()

            fs_start_alt = fs.position[:, 2].copy()  # hover layers launch_sequence left the fleet on
            snapshots: list[tuple[float, np.ndarray]] = []
            breached_ever = False
            t0 = time.perf_counter()
            sim_t0 = fs.time_s
            manager.start_trajectory(show, mode="LOCAL")
            while time.perf_counter() - t0 < duration + 1.5:
                await asyncio.sleep(1.0 / SAMPLE_HZ)
                # The sim reports altitude up (+z); the show file is NED (-z up).
                pos = fs.position.copy()
                pos[:, 2] = -pos[:, 2]
                pos[:, 0] += pad[:, 0]
                pos[:, 1] += pad[:, 1]
                # The mission timeline runs off the drones' UTC clock, not the sim tick count
                # (tara_sim FleetState.utc_clock), so the plan is read against that same clock.
                # Late ticks then show up as the drones lagging the plan, which is what they do.
                started = fs.mission_start_utc[~np.isnan(fs.mission_start_utc)]
                plan_t = fs.utc_clock() - float(np.median(started)) if started.size else -1.0
                snapshots.append((plan_t, pos))
                breached_ever = breached_ever or any(manager.proximity_breaches.values())
            phases["show_s"] = time.perf_counter() - t0
            await probe.stop()

            t = recorder.summary()
            t["sim_realtime_ratio"] = round((fs.time_s - sim_t0) / phases["show_s"], 3)
            result["tick"] = t
            result["control_loop"] = probe.summary()
            result["asyncio_tasks_peak"] = tasks.peak
            result["proximity_breach_seen_by_gcs_monitor"] = breached_ever
            result["geofence_breaches"] = sum(1 for v in manager.geofence_breaches.values() if v)

            # Post-process the snapshots after the show, off the event loop's critical path.
            #
            # launch_sequence hovers every drone on its own altitude layer, and at 100+ drones
            # those layers cycle up to the 120 m ceiling (tara_core.altitude_layers). A Studio
            # show starts on the pad at t=0, so the onboard start first sends the whole fleet
            # down to its pad waypoint. That transient is a property of the GCS launch path,
            # not of the show, so tracking and separation are reported both overall and after
            # the fleet has settled onto the plan (fleet-max error under SETTLE_ERR_M).
            series = []  # (show time, per-drone error, positions)
            for st, pos in snapshots:
                if 0 <= st <= duration:
                    series.append((st, np.linalg.norm(pos - planned_at(times, xyz, st), axis=1), pos))
            # Settled = the first sample from which the fleet-max error stays under
            # SETTLE_ERR_M for the next SETTLE_WINDOW_S. Later excursions still count: they
            # are in the after-settle maximum rather than hidden by it.
            window = int(SETTLE_WINDOW_S * SAMPLE_HZ)
            first_settled = next(
                (i for i in range(len(series)) if all(e.max() < SETTLE_ERR_M for _, e, _ in series[i:i + window])),
                len(series),
            )
            result["settled_at_s"] = round(series[first_settled][0], 1) if first_settled < len(series) else None
            if raw_out is not None:
                np.savez_compressed(raw_out, t=np.array([st for st, _ in snapshots]),
                                    pos=np.array([p for _, p in snapshots], dtype=np.float32), ids=np.array(ids))
            result["altitude_at_show_start_m"] = {
                "min": round(float(fs_start_alt.min()), 1),
                "max": round(float(fs_start_alt.max()), 1),
            }
            for label, chunk in (("before_settle", series[:first_settled]), ("after_settle", series[first_settled:])):
                if not chunk:
                    continue
                flat = np.concatenate([e for _, e, _ in chunk])
                result[f"tracking_error_{label}_m"] = {
                    "mean": round(float(flat.mean()), 3), "p99": round(float(np.percentile(flat, 99)), 3),
                    "max": round(float(flat.max()), 3),
                }
                result[f"flown_min_separation_{label}_m"] = round(min(min_pairwise(p) for _, _, p in chunk), 3)
                result[f"max_altitude_{label}_m"] = round(max(float((-p[:, 2]).max()) for _, _, p in chunk), 2)
            result["sampled_at_hz"] = SAMPLE_HZ
            result["on_ground_at_end"] = int((fs.position[:, 2] < 0.5).sum())
        finally:
            if recorder is not None:
                recorder.remove()
            await probe.stop()
            await manager.shutdown()
            tasks.remove()

    result["phases_s"] = {k: round(v, 1) for k, v in phases.items()}
    result["resources"] = sampler.summary()
    t, c = result["tick"], result["control_loop"]
    result["acceptance_51"] = {
        "tasks_under_50": result["asyncio_tasks_peak"] < bfs.TASK_BUDGET,
        "control_latency_under_50ms": c.get("lateness_ms_max", float("inf")) < bfs.CONTROL_LATENCY_BUDGET_MS,
        "no_missed_ticks": c.get("missed_pct", 100.0) == 0.0 and t["late_tick_pct"] == 0.0,
        "realtime_factor_at_least_0_98": t["sim_realtime_ratio"] >= bfs.MIN_REALTIME_FACTOR,
    }
    print(json.dumps(result, indent=2), flush=True)
    return result


async def main_async(files: list[Path], out: Path | None, raw_dir: Path | None) -> None:
    results = [await fly(f, raw_dir / f"{f.stem}.samples.npz" if raw_dir else None) for f in files]
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nwrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("shows", nargs="+", type=Path, help="exported show JSON(s) from tools/diwali_build.mjs")
    ap.add_argument("--out", type=Path, default=None, help="write all results to this JSON file")
    ap.add_argument("--raw-dir", type=Path, default=None, help="also save the sampled positions (npz) here")
    args = ap.parse_args()
    if args.raw_dir:
        args.raw_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(main_async(args.shows, args.out, args.raw_dir))


if __name__ == "__main__":
    main()
