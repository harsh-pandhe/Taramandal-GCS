#!/usr/bin/env python3
"""Profiles why `tara_sim` falls behind real time while it flies a large show.

`tools/benchmark_fleet_scale.py` flies a 4-waypoint, 15 s synthetic show and holds a
real-time factor (RTF) of ~1.00 at 500 drones. `tools/diwali_sim_scale.py` flies the
certified Diwali shows through the same `DroneManager` path and gets 0.98 / 0.93 / 0.78 at
35 / 100 / 500 drones. This script flies the same shows and says where the missing wall-clock
time goes. It touches no product code: everything is measured from outside, by wrapping
bound methods on the live instances (the way `bfs.TickLatencyRecorder` does) and by patching
`asyncio.events.Handle._run` in this process only.

Two profilers, run separately because cProfile's per-call overhead distorts a loop made of
many small calls:

- `--profiler steps` (default, low overhead). Every event-loop callback is timed and
  charged to the coroutine that owns it, so the show window is split by *who held the loop*:
  the physics tick, the onboard mission executor, bulk telemetry ingest, the proximity
  monitor, the show-state snapshot, ... It also reports the slowest single steps with their
  offset into the show (a stall that repeats every 5 s is a periodic task, not load), GC
  pauses per generation, per-window RTF, and where each tick's own time goes.
- `--profiler cprofile`. cProfile over the show window only; top functions by self time.

`--disable` switches individual GCS background tasks off *for the experiment only* (instance
attribute no-ops / task cancellation here, never a product edit) so a suspected cost can be
confirmed by removing it. A run that disables anything is a counterfactual, not a baseline.

Docker guard: Gazebo's RTF depends on host CPU. Before a run this waits until no
`tara-sitl-*` container is running (`docker ps`), and during the run it keeps checking; a run
that overlapped a SITL container is written with `"valid": false` and should be discarded.

Usage, from the repo root:

    python tools/profile_show_rtf.py dist/diwali/diwali_100.json --out out/rtf_100_steps.json
    python tools/profile_show_rtf.py dist/diwali/diwali_500.json --profiler cprofile --out out/rtf_500_cprofile.json
    python tools/profile_show_rtf.py dist/diwali/diwali_500.json --disable snapshot --out out/rtf_500_nosnap.json
    python tools/profile_show_rtf.py --synthetic 500 --duration 216 --rate-hz 10 --out out/rtf_500_dense.json
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import gc
import io
import json
import math
import pstats
import shutil
import statistics
import subprocess
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
for _pkg in ("tara-core", "tara-gcs", "tara-sim"):
    sys.path.insert(0, str(_ROOT / "packages" / _pkg))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import benchmark_fleet_scale as bfs  # noqa: E402
from tara_gcs.drone_manager import DroneManager, _pad_offset  # noqa: E402

SAMPLE_HZ = 5.0  # the position sampler diwali_sim_scale.py runs on the loop during the show
LONG_STEP_MS = 25.0  # a single loop callback this long delays the next 20 ms tick
SITL_PREFIX = "tara-sitl-"
SITL_POLL_S = 5.0

#: Disable switches. Each maps to a coroutine qualname to cancel, or a special-case no-op.
_CANCEL_TASKS = {
    "proximity": "DroneManager._proximity_monitor_loop",
    "geofence": "DroneManager._geofence_monitor_loop",
    "lifecycle": "DroneManager._lifecycle_monitor_loop",
}
DISABLE_CHOICES = ("snapshot", "recording", "sampler", *_CANCEL_TASKS)


# --------------------------------------------------------------------------- docker guard


def running_sitl_containers() -> list[str]:
    """Names of running `tara-sitl-*` containers; empty when none (or no docker CLI)."""
    if shutil.which("docker") is None:
        return []
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=20, check=True
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"[guard] docker ps failed ({exc}); treating as 'cannot tell'", file=sys.stderr)
        return ["<docker ps failed>"]
    return [n for n in out.split() if n.startswith(SITL_PREFIX)]


def wait_for_no_sitl() -> float:
    """Blocks until no SITL container runs. Returns seconds waited."""
    t0 = time.monotonic()
    last_note = -math.inf
    while True:
        names = running_sitl_containers()
        if not names:
            return time.monotonic() - t0
        if time.monotonic() - last_note >= 300.0:
            print(f"[guard] waiting for SITL containers to stop: {', '.join(names)}", flush=True)
            last_note = time.monotonic()
        time.sleep(SITL_POLL_S)


class SitlWatch:
    """Background thread: notes if a SITL container shows up while a run is in progress."""

    def __init__(self) -> None:
        self.seen: set[str] = set()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> "SitlWatch":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=30)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.seen.update(running_sitl_containers())
            self._stop.wait(SITL_POLL_S)


# --------------------------------------------------------------------------- step profiler


class LoopStepProfiler:
    """Times every event-loop callback and charges it to the coroutine that owns it."""

    def __init__(self) -> None:
        self.reset()
        self._orig = None

    def reset(self) -> None:
        self.total_s: dict[str, float] = defaultdict(float)
        self.long_s: dict[str, float] = defaultdict(float)  # time in steps >= LONG_STEP_MS
        self.count: dict[str, int] = defaultdict(int)
        self.max_s: dict[str, float] = defaultdict(float)
        self.long_steps: list[tuple[float, str, float]] = []  # (offset_s, owner, dur_s)
        self.t0 = time.perf_counter()

    @staticmethod
    def owner_of(handle) -> str:
        cb = handle._callback
        target = getattr(cb, "__self__", None)
        if isinstance(target, asyncio.Task):
            coro = target.get_coro()
            return getattr(coro, "__qualname__", None) or type(coro).__name__
        return "<loop callback> " + (getattr(cb, "__qualname__", None) or type(cb).__name__)

    def install(self) -> None:
        orig = asyncio.events.Handle._run
        self._orig = orig
        prof = self
        clock = time.perf_counter

        def timed_run(handle):
            start = clock()
            try:
                orig(handle)
            finally:
                dur = clock() - start
                owner = prof.owner_of(handle)
                prof.total_s[owner] += dur
                prof.count[owner] += 1
                if dur > prof.max_s[owner]:
                    prof.max_s[owner] = dur
                if dur * 1000.0 >= LONG_STEP_MS:
                    prof.long_s[owner] += dur
                    prof.long_steps.append((start - prof.t0, owner, dur))

        asyncio.events.Handle._run = timed_run

    def remove(self) -> None:
        if self._orig is not None:
            asyncio.events.Handle._run = self._orig
            self._orig = None


class GcWatch:
    def __init__(self) -> None:
        self.reset()
        self._start = 0.0

    def reset(self) -> None:
        self.pauses: dict[int, list[float]] = {0: [], 1: [], 2: []}
        self.long: list[tuple[int, float]] = []  # (generation, seconds)
        self.t0 = time.perf_counter()

    def _cb(self, phase: str, info: dict) -> None:
        if phase == "start":
            self._start = time.perf_counter()
        else:
            dur = time.perf_counter() - self._start
            self.pauses[info["generation"]].append(dur)
            if dur * 1000.0 >= 10.0:
                self.long.append((info["generation"], dur))

    def install(self) -> None:
        gc.callbacks.append(self._cb)

    def remove(self) -> None:
        if self._cb in gc.callbacks:
            gc.callbacks.remove(self._cb)

    def summary(self) -> dict:
        out = {}
        for gen, xs in self.pauses.items():
            out[f"gen{gen}"] = {
                "collections": len(xs),
                "total_ms": round(sum(xs) * 1000.0, 1),
                "max_ms": round(max(xs) * 1000.0, 2) if xs else 0.0,
            }
        out["pauses_over_10ms"] = [{"gen": g, "ms": round(s * 1000.0, 1)} for g, s in self.long[:20]]
        return out


class TickSplit:
    """Wraps `FleetState.tick` and its sub-steps on the live instance (restores on remove)."""

    SUBSTEPS = ("_apply_mission", "_apply_offboard_loss_failsafe", "_apply_landing_and_rtl")

    def __init__(self, fs) -> None:
        self.fs = fs
        self.reset()

    def reset(self) -> None:
        self.starts: list[float] = []
        self.durs: list[float] = []
        self.sub: dict[str, float] = {name: 0.0 for name in self.SUBSTEPS}
        self.sim_times: list[float] = []

    def install(self) -> None:
        fs, clock = self.fs, time.perf_counter
        orig_tick = fs.tick

        def timed_tick(dt):
            s = clock()
            orig_tick(dt)
            e = clock()
            self.starts.append(s)
            self.durs.append(e - s)
            self.sim_times.append(fs.time_s)

        fs.tick = timed_tick
        for name in self.SUBSTEPS:
            orig = getattr(fs, name)

            def wrapped(*a, _orig=orig, _name=name, **kw):
                s = clock()
                try:
                    return _orig(*a, **kw)
                finally:
                    self.sub[_name] += clock() - s

            setattr(fs, name, wrapped)

    def remove(self) -> None:
        for name in ("tick", *self.SUBSTEPS):
            self.fs.__dict__.pop(name, None)


class CallTimer:
    """Accumulates wall time spent inside one bound method of a live object."""

    def __init__(self, obj, name: str) -> None:
        self.obj, self.name = obj, name
        self.total_s = 0.0
        self.calls = 0
        self.max_s = 0.0

    def install(self) -> None:
        orig, clock = getattr(self.obj, self.name), time.perf_counter

        def wrapped(*a, **kw):
            s = clock()
            try:
                return orig(*a, **kw)
            finally:
                d = clock() - s
                self.total_s += d
                self.calls += 1
                self.max_s = max(self.max_s, d)

        setattr(self.obj, self.name, wrapped)

    def remove(self) -> None:
        self.obj.__dict__.pop(self.name, None)

    def reset(self) -> None:
        self.total_s, self.calls, self.max_s = 0.0, 0, 0.0

    def summary(self) -> dict:
        return {"calls": self.calls, "total_s": round(self.total_s, 3), "max_ms": round(self.max_s * 1000.0, 2)}


# --------------------------------------------------------------------------- shows


def load_show(path: Path) -> dict[int, list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {int(d["id"]): d["waypoints"] for d in data["drones"]}


def dense_synthetic_show(n: int, duration: float, rate_hz: float) -> dict[int, list[dict]]:
    """`bfs.benchmark_show`'s climb/hold/drift, optionally resampled to `rate_hz` waypoints.

    `rate_hz == 0` is the stock 4-waypoint show. Otherwise the same path is sampled at
    `rate_hz`, so waypoint count per drone matches a Studio export of the same duration while
    everything else (no formations, same pads and layers) stays the synthetic benchmark's.
    """
    base = bfs.benchmark_show(n, duration)
    if rate_hz <= 0:
        return base
    steps = int(round(duration * rate_hz))
    times = np.linspace(0.0, duration, steps + 1)
    show: dict[int, list[dict]] = {}
    for i, wps in base.items():
        bt = np.array([w["time"] for w in wps])
        cols = {k: np.interp(times, bt, [w[k] for w in wps]) for k in ("x", "y", "z", "yaw")}
        show[i] = [
            {"time": round(float(t), 4), "x": round(float(cols["x"][j]), 3), "y": round(float(cols["y"][j]), 3),
             "z": round(float(cols["z"][j]), 3), "yaw": round(float(cols["yaw"][j]), 2)}
            for j, t in enumerate(times)
        ]
    return show


# --------------------------------------------------------------------------- run


def pct(xs: list[float] | np.ndarray, q: float) -> float:
    return float(np.percentile(np.asarray(xs), q)) if len(xs) else 0.0


async def fly(label: str, show: dict[int, list[dict]], profiler: str, disable: set[str], pad_sampler: bool) -> dict:
    n = len(show)
    ids = sorted(show)
    duration = float(show[ids[0]][-1]["time"])
    wps_per_drone = len(show[ids[0]])
    print(f"\n=== {label}: N={n}, {wps_per_drone} waypoints/drone, planned {duration:.1f}s, "
          f"profiler={profiler}, disable={sorted(disable) or '-'} ===", flush=True)

    manager = DroneManager(ports=list(range(n)), backend="sim")
    tasks = bfs.TaskCounter(asyncio.get_running_loop())
    tasks.install()
    probe = bfs.ControlLoopProbe()
    result: dict = {"label": label, "n": n, "waypoints_per_drone": wps_per_drone,
                    "planned_duration_s": round(duration, 2), "profiler": profiler, "disabled": sorted(disable)}
    phases: dict[str, float] = {}
    pad = np.array([_pad_offset(i) for i in ids])
    steps = LoopStepProfiler() if profiler == "steps" else None
    gcw = GcWatch()
    cprof = cProfile.Profile() if profiler == "cprofile" else None
    timers: dict[str, CallTimer] = {}
    split: TickSplit | None = None

    with bfs.ResourceSampler() as sampler:
        try:
            await manager.connect_all()
            fs = manager._sim_fleet.state
            await asyncio.sleep(0.5)

            if "snapshot" in disable:
                manager._write_state_snapshot = lambda: None
            if "recording" in disable:
                manager.start_telemetry_recording = lambda: None
            for key in disable & set(_CANCEL_TASKS):
                for task in list(manager._tasks):
                    coro = task.get_coro() if isinstance(task, asyncio.Task) else None
                    if coro is not None and coro.__qualname__ == _CANCEL_TASKS[key]:
                        task.cancel()
            await asyncio.sleep(0)

            if steps is not None:
                split = TickSplit(fs)
                split.install()
                timers["bulk_ingest"] = CallTimer(manager, "_ingest_bulk_telemetry")
                timers["bulk_read"] = CallTimer(manager._sim_fleet, "get_bulk_telemetry")
                timers["state_snapshot_write"] = CallTimer(manager, "_write_state_snapshot")
                for t in timers.values():
                    t.install()

            t0 = time.perf_counter()
            await manager.upload_show(show)
            phases["upload_s"] = time.perf_counter() - t0
            t0 = time.perf_counter()
            await manager.launch_sequence()
            phases["launch_s"] = time.perf_counter() - t0
            await asyncio.sleep(0.5)
            result["armed_in_physics"] = int(fs.armed.sum())

            # ---- show window: everything below is what gets profiled -----------------
            probe.start()
            tasks.reset()
            gc.collect()  # a clean start, as a long-running process would have had many by now
            gcw.reset()
            if split is not None:
                split.reset()
                for t in timers.values():
                    t.reset()
            if steps is not None:
                steps.install()
                steps.reset()
            gcw.install()
            if cprof is not None:
                cprof.enable()

            snapshots = 0
            t0 = time.perf_counter()
            sim_t0 = fs.time_s
            manager.start_trajectory(show, mode="LOCAL")
            # Same sampling the Diwali tool does on the loop, so the loop carries the same work.
            while time.perf_counter() - t0 < duration + 1.5:
                await asyncio.sleep(1.0 / SAMPLE_HZ)
                if pad_sampler:
                    pos = fs.position.copy()
                    pos[:, 2] = -pos[:, 2]
                    pos[:, 0] += pad[:, 0]
                    pos[:, 1] += pad[:, 1]
                    started = fs.mission_start_utc[~np.isnan(fs.mission_start_utc)]
                    _ = fs.utc_clock() - float(np.median(started)) if started.size else -1.0
                    snapshots += 1
            show_s = time.perf_counter() - t0
            sim_dt = fs.time_s - sim_t0
            if cprof is not None:
                cprof.disable()
            gcw.remove()
            if steps is not None:
                steps.remove()
            await probe.stop()
            phases["show_s"] = show_s

            result["show_window_s"] = round(show_s, 2)
            result["sim_s_advanced"] = round(sim_dt, 2)
            result["rtf"] = round(sim_dt / show_s, 4)
            result["lost_wall_s"] = round(show_s - sim_dt, 2)
            result["control_loop"] = probe.summary()
            result["asyncio_tasks_peak"] = tasks.peak
            result["gc"] = gcw.summary()

            if split is not None and steps is not None:
                result.update(_summarise_steps(steps, split, timers, show_s, sim_dt))
            if cprof is not None:
                result["cprofile"] = _summarise_cprofile(cprof)
        finally:
            gcw.remove()
            if steps is not None:
                steps.remove()
            if split is not None:
                split.remove()
            for t in timers.values():
                t.remove()
            await probe.stop()
            await manager.shutdown()
            tasks.remove()

    result["phases_s"] = {k: round(v, 1) for k, v in phases.items()}
    result["resources"] = sampler.summary()
    return result


def _summarise_steps(steps: LoopStepProfiler, split: TickSplit, timers: dict, show_s: float, sim_dt: float) -> dict:
    durs = np.array(split.durs)
    starts = np.array(split.starts)
    out: dict = {}

    owners = []
    busy = 0.0
    for owner, tot in sorted(steps.total_s.items(), key=lambda kv: -kv[1]):
        busy += tot
        owners.append({
            "owner": owner, "total_s": round(tot, 2), "pct_of_wall": round(100.0 * tot / show_s, 2),
            "steps": steps.count[owner], "mean_ms": round(1000.0 * tot / steps.count[owner], 3),
            "max_ms": round(steps.max_s[owner] * 1000.0, 1),
            "time_in_steps_ge_25ms_s": round(steps.long_s.get(owner, 0.0), 2),
        })
    out["loop_owners"] = owners[:12]
    out["loop_busy_pct"] = round(100.0 * busy / show_s, 1)

    if durs.size:
        gaps = np.diff(starts) * 1000.0
        out["tick"] = {
            "ticks": int(durs.size),
            "ticks_expected_at_rtf1": int(round(show_s / 0.02)),
            "tick_cost_ms": {"mean": round(float(durs.mean()) * 1000, 3), "p50": round(pct(durs, 50) * 1000, 3),
                             "p95": round(pct(durs, 95) * 1000, 3), "p99": round(pct(durs, 99) * 1000, 3),
                             "max": round(float(durs.max()) * 1000, 2)},
            "gap_ms": {"mean": round(float(gaps.mean()), 2), "p50": round(pct(gaps, 50), 2),
                       "p95": round(pct(gaps, 95), 2), "p99": round(pct(gaps, 99), 2),
                       "max": round(float(gaps.max()), 1)},
            "gaps_over_100ms": int((gaps > 100.0).sum()),
            "wall_in_gaps_over_100ms_s": round(float(gaps[gaps > 100.0].sum()) / 1000.0, 2),
            "substeps_ms_per_tick": {k: round(v * 1000.0 / durs.size, 3) for k, v in split.sub.items()},
        }
        other = float(durs.mean()) * 1000.0 - sum(out["tick"]["substeps_ms_per_tick"].values())
        out["tick"]["numpy_physics_ms_per_tick"] = round(other, 3)

        # RTF per 10 s window of wall time: does it sag steadily or in lumps?
        sim = np.array(split.sim_times)
        win = 10.0
        edges = np.arange(starts[0], starts[-1] + win, win)
        idx = np.searchsorted(starts, edges)
        series = []
        for a, b in zip(idx[:-1], idx[1:]):
            if b - a >= 1:
                span_wall = starts[min(b, len(starts) - 1)] - starts[a]
                if span_wall > 0:
                    series.append(round(float((sim[min(b, len(sim) - 1)] - sim[a]) / span_wall), 3))
        out["rtf_per_10s_window"] = series

    out["timed_calls"] = {k: v.summary() for k, v in timers.items()}
    longest = sorted(steps.long_steps, key=lambda s: -s[2])[:12]
    out["longest_steps"] = [{"at_s": round(o, 1), "owner": w, "ms": round(d * 1000.0, 1)} for o, w, d in longest]
    # Periodicity of the stalls: spacing between consecutive long steps of the worst owner.
    if steps.long_steps:
        worst_owner = max(steps.long_s, key=steps.long_s.get)
        offs = sorted(o for o, w, _ in steps.long_steps if w == worst_owner)
        out["worst_long_step_owner"] = worst_owner
        out["worst_owner_long_steps"] = len(offs)
        if len(offs) > 2:
            out["worst_owner_spacing_s_median"] = round(statistics.median(np.diff(offs)), 2)
    return out


def _summarise_cprofile(cprof: cProfile.Profile) -> dict:
    stats = pstats.Stats(cprof)
    stats.sort_stats("tottime")
    buf = io.StringIO()
    stats.stream = buf
    stats.print_stats(30)
    top_self = []
    for (fname, line, func), (cc, nc, tt, ct, _callers) in stats.stats.items():
        top_self.append((tt, ct, nc, f"{Path(fname).name}:{line}({func})"))
    top_self.sort(reverse=True)
    top_cum = sorted(top_self, key=lambda r: -r[1])

    def fmt(rows):
        return [{"fn": r[3], "self_s": round(r[0], 3), "cum_s": round(r[1], 3), "calls": r[2]} for r in rows]

    return {"top_by_self_time": fmt(top_self[:25]), "top_by_cumulative": fmt(top_cum[:25]), "total_s": round(stats.total_tt, 2)}


def print_summary(r: dict) -> None:
    print(f"\n--- {r['label']} ---  RTF {r['rtf']}  (lost {r['lost_wall_s']} s of {r['show_window_s']} s wall)")
    for o in r.get("loop_owners", [])[:8]:
        print(f"  {o['pct_of_wall']:6.2f}%  {o['total_s']:8.2f}s  max {o['max_ms']:8.1f}ms  {o['owner']}")
    if "tick" in r:
        t = r["tick"]
        print(f"  tick: {t['ticks']} (expected {t['ticks_expected_at_rtf1']}), cost mean {t['tick_cost_ms']['mean']} ms, "
              f"gaps>100ms: {t['gaps_over_100ms']} = {t['wall_in_gaps_over_100ms_s']} s of wall")
    for o in r.get("longest_steps", [])[:5]:
        print(f"  long step @ {o['at_s']}s: {o['ms']} ms  {o['owner']}")
    if "cprofile" in r:
        for row in r["cprofile"]["top_by_self_time"][:10]:
            print(f"  {row['self_s']:8.3f}s self  {row['calls']:>9} calls  {row['fn']}")


async def amain(args: argparse.Namespace) -> None:
    disable = set(args.disable.split(",")) if args.disable else set()
    bad = disable - set(DISABLE_CHOICES)
    if bad:
        sys.exit(f"unknown --disable value(s): {sorted(bad)}; choose from {DISABLE_CHOICES}")

    waited = wait_for_no_sitl()
    if waited > 0:
        print(f"[guard] waited {waited:.0f}s for SITL containers to stop", flush=True)

    if args.synthetic:
        label = f"synthetic_{args.synthetic}_{args.duration:g}s_{args.rate_hz:g}hz"
        show = dense_synthetic_show(args.synthetic, args.duration, args.rate_hz)
    else:
        label = args.show.stem
        show = load_show(args.show)

    with SitlWatch() as watch:
        result = await fly(label, show, args.profiler, disable, not args.no_sampler)
    result["sitl_waited_s"] = round(waited, 1)
    result["sitl_seen_during_run"] = sorted(watch.seen)
    result["valid"] = not watch.seen
    if not result["valid"]:
        print(f"[guard] SITL container(s) {sorted(watch.seen)} ran during this run: INVALID, discard.", flush=True)
    print_summary(result)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"wrote {args.out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("show", nargs="?", type=Path, help="exported show JSON (tools/diwali_build.mjs output)")
    ap.add_argument("--synthetic", type=int, default=0, metavar="N", help="fly benchmark_show for N drones instead")
    ap.add_argument("--duration", type=float, default=15.0, help="synthetic show duration, s")
    ap.add_argument("--rate-hz", type=float, default=0.0, help="synthetic waypoint rate; 0 = the stock 4 waypoints")
    ap.add_argument("--profiler", choices=("steps", "cprofile"), default="steps")
    ap.add_argument("--disable", default="", help=f"comma list for counterfactual runs: {', '.join(DISABLE_CHOICES)}")
    ap.add_argument("--no-sampler", action="store_true", help="skip the 5 Hz position sampler the Diwali tool runs")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    if not args.show and not args.synthetic:
        ap.error("give a show file or --synthetic N")
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
