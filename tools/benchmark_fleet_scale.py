#!/usr/bin/env python3
"""Measures where `tara-sim`'s single-process vectorized fleet actually stops
scaling — the real ceiling, not the theoretical `MAX_FLEET_SIZE = 1000`.

The vectorized design's central claim is that per-tick cost stays roughly flat
as the fleet grows, because the whole fleet shares one numpy tick instead of
one asyncio task per drone. `packages/tara-gcs/tests/test_sim_backend.py`
proves 100 drones fly *correctly*; it says nothing about what that costs, or
what happens at 500 and 1000. This script produces those numbers.

What it measures, per fleet size:

- **Wall-clock**, split into connect / launch / show phases. Splitting matters:
  `launch_sequence` deliberately staggers drones 1s apart in batches of
  `LAUNCH_BATCH_SIZE`, so its duration is a fixed function of fleet size
  (`ceil(n / 5) * 4` seconds — the five drones in a batch sleep 0/1/2/3/4s
  concurrently under one `gather`, so a batch costs 4s, not 5s) and has
  nothing to do with computational load. Rolled
  into one total it would swamp every other number and make the fleet look like
  it scales far worse than it does.
- **Tick-loop latency** — the headline metric. `FleetState._run` targets one
  tick every `config.dt` (20ms at the default 50Hz). Actual tick-to-tick spacing
  above that is the event loop failing to come back in time, which is the real
  saturation signal: it means physics is running slower than wall-clock and the
  simulated fleet is no longer flying in real time. Reported as mean/p50/p95/max
  and as overshoot past the measured idle floor.

  That floor is not always `dt`. On Windows the default timer resolution is
  ~15.6ms, so `asyncio.sleep(0.02)` actually returns after ~31ms even on a
  completely idle loop — measured, not assumed; see `measure_idle_floor` below,
  which the run calls before touching a fleet. Scoring overshoot against the
  nominal 20ms there would report ~11ms of "lateness" at *every* fleet size
  including n=0, which looks like saturation and is really just the platform
  clock. Overshoot is therefore measured against `max(dt, idle floor)`, so the
  number means "cost attributable to fleet size" on any host.
- **CPU%** (process, across the whole run) and **peak RSS**, sampled from a
  background thread — not the asyncio loop, which is the thing under test and
  would report its own starvation as low CPU.

Both metrics are collected against the real `DroneManager` on `backend="sim"` —
the same connect / launch / trajectory-playback code path production uses, with
its 8 per-drone monitor tasks intact. Benchmarking `FleetState` alone would
measure the one component already known to be cheap and miss where the cost
actually lands.

Usage, from the repo root:

    .venv/Scripts/python tools/benchmark_fleet_scale.py
    .venv/Scripts/python tools/benchmark_fleet_scale.py -n 100,500,1000
    .venv/Scripts/python tools/benchmark_fleet_scale.py -n 750 --show-duration 20

Results are written to `tools/benchmark_results.json` alongside a printed
summary table. See `docs/fleet-scale-benchmark.md` for the measured ceiling and
what breaks first past it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _pkg in ("tara-gcs", "tara-core", "tara-sim"):
    sys.path.insert(0, str(_ROOT / "packages" / _pkg))

import psutil  # noqa: E402

from tara_core import altitude_layers  # noqa: E402
from tara_gcs.drone_manager import DroneManager, _pad_offset  # noqa: E402
from tara_sim import SimConfig  # noqa: E402

DEFAULT_SIZES = (100, 500, 1000)
DEFAULT_SHOW_DURATION_S = 15.0

#: Sampling period for the CPU/RSS poller thread.
_RESOURCE_SAMPLE_S = 0.25

#: A tick-to-tick gap above dt * this factor is counted as a "late" tick. 1.5x
#: (30ms at the default 50Hz) is loose enough not to fire on ordinary OS
#: scheduling jitter, tight enough that sustained lateness means the loop is
#: genuinely not keeping up.
_LATE_TICK_FACTOR = 1.5


class ResourceSampler:
    """Polls process CPU% and RSS from a plain thread.

    Deliberately not an asyncio task: the event loop is the subject of the
    measurement, so a sampler living on it would stop being scheduled at
    exactly the moment its readings matter most, and would under-report the
    saturation it exists to detect.
    """

    def __init__(self, interval_s: float = _RESOURCE_SAMPLE_S):
        self.interval_s = interval_s
        self._proc = psutil.Process()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.cpu_samples: list[float] = []
        self.rss_samples_mb: list[float] = []

    def __enter__(self) -> "ResourceSampler":
        self._proc.cpu_percent(None)  # prime; the first call always returns 0.0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.cpu_samples.append(self._proc.cpu_percent(None))
                self.rss_samples_mb.append(self._proc.memory_info().rss / (1024 * 1024))
            except psutil.Error:  # process died mid-sample; nothing useful left to record
                return

    def summary(self) -> dict:
        cpu = [c for c in self.cpu_samples if c > 0.0]
        return {
            "cpu_percent_mean": round(statistics.fmean(cpu), 1) if cpu else 0.0,
            "cpu_percent_peak": round(max(cpu), 1) if cpu else 0.0,
            "rss_mb_peak": round(max(self.rss_samples_mb), 1) if self.rss_samples_mb else 0.0,
            "samples": len(self.cpu_samples),
        }


async def measure_idle_floor(dt: float, samples: int = 120) -> float:
    """Returns the mean tick-to-tick gap an empty event loop achieves for ``dt``.

    This is the best `FleetState._run`'s ``await asyncio.sleep(dt)`` can
    possibly do on this host with no fleet at all, and it is not always ``dt``:
    Windows' default ~15.6ms timer granularity rounds a 20ms sleep up to ~31ms.
    Subtracting this floor is what separates "the fleet is too big" from "the OS
    clock is coarse" — without it every result on Windows reports ~11ms of
    phantom lateness that no amount of optimization could remove.
    """
    gaps: list[float] = []
    last: float | None = None
    for _ in range(samples):
        await asyncio.sleep(dt)
        now = time.perf_counter()
        if last is not None:
            gaps.append((now - last) * 1000.0)
        last = now
    return statistics.fmean(gaps)


class TickLatencyRecorder:
    """Wraps `FleetState.tick` to record actual tick-to-tick wall-clock spacing.

    Measures the gap *between* consecutive ticks rather than the duration of
    `tick()` itself. The numpy work is known to be cheap; the question this
    benchmark exists to answer is whether the event loop gets back to the tick
    task on schedule once thousands of per-drone monitor tasks compete with it.
    Only the gap captures that.
    """

    def __init__(self, fleet_state, dt: float, idle_floor_ms: float | None = None):
        self.dt = dt
        # What this host can actually achieve for a dt-second sleep. Overshoot
        # is scored against this rather than the nominal dt so the number
        # reflects fleet cost, not OS timer granularity.
        self.floor_ms = max(dt * 1000.0, idle_floor_ms or 0.0)
        self.gaps_ms: list[float] = []
        self._fs = fleet_state
        self._orig_tick = fleet_state.tick
        self._last: float | None = None

    def install(self) -> None:
        def timed_tick(dt_arg: float) -> None:
            now = time.perf_counter()
            if self._last is not None:
                self.gaps_ms.append((now - self._last) * 1000.0)
            self._last = now
            self._orig_tick(dt_arg)

        self._fs.tick = timed_tick

    def remove(self) -> None:
        # Delete the instance attribute rather than reassigning `_orig_tick`
        # onto it: `_orig_tick` is a *bound method*, so assigning it back would
        # leave a permanent instance-level shadow of the class's own `tick`
        # holding a reference to this FleetState. Deleting re-exposes the
        # class attribute, leaving the object exactly as it was found.
        self._fs.__dict__.pop("tick", None)

    def reset(self) -> None:
        """Drop samples collected so far and restart the gap clock."""
        self.gaps_ms.clear()
        self._last = None

    def summary(self) -> dict:
        dt_ms = self.dt * 1000.0
        if not self.gaps_ms:
            return {"ticks": 0}
        gaps = sorted(self.gaps_ms)
        # Overshoot past what this host can actually achieve is the interesting
        # quantity: a loop keeping up sits at ~0 here regardless of tick_hz or
        # the platform's timer granularity.
        over = sorted(max(0.0, g - self.floor_ms) for g in self.gaps_ms)
        late = sum(1 for g in self.gaps_ms if g > self.floor_ms * _LATE_TICK_FACTOR)
        return {
            "ticks": len(gaps),
            "target_dt_ms": round(dt_ms, 2),
            "idle_floor_ms": round(self.floor_ms, 2),
            "gap_ms_mean": round(statistics.fmean(gaps), 2),
            "gap_ms_p50": round(gaps[len(gaps) // 2], 2),
            "gap_ms_p95": round(gaps[int(len(gaps) * 0.95)], 2),
            "gap_ms_max": round(gaps[-1], 2),
            "overshoot_ms_mean": round(statistics.fmean(over), 2),
            "overshoot_ms_p95": round(over[int(len(over) * 0.95)], 2),
            "late_tick_pct": round(100.0 * late / len(gaps), 1),
            "effective_tick_hz": round(1000.0 / statistics.fmean(gaps), 1),
            # Physics falls behind wall-clock once the real gap exceeds dt:
            # FleetState.tick always advances time by dt regardless of how long
            # the loop actually took, so this ratio is how much slower than real
            # time the simulated fleet is flying.
            "realtime_ratio": round(dt_ms / statistics.fmean(gaps), 3),
        }


def benchmark_show(n: int, duration: float) -> dict:
    """A climb / hold / drift trajectory, one entry per drone.

    Mirrors `test_sim_backend._short_safe_show`: pad positions come from
    `_pad_offset` (the fixed `MAX_FLEET_SIZE` grid `DroneManager` itself
    converts against), never a freshly-sized `launch_grid(n)` — those disagree
    for any n below the max, and using the wrong one silently reshuffles drones
    onto each other's pads. The lateral drift in the final leg keeps the
    trajectory interpolator and the velocity-feedforward setpoint path doing
    real work every tick instead of resending a constant hover setpoint.
    """
    alts = altitude_layers(n)
    climb_t, hold_t = duration * 0.25, duration * 0.6
    show = {}
    for i in range(n):
        x, y = _pad_offset(i)
        alt = alts[i]
        show[i] = [
            {"time": 0.0, "x": x, "y": y, "z": 0.0, "yaw": 0.0},
            {"time": climb_t, "x": x, "y": y, "z": -alt, "yaw": 0.0},
            {"time": hold_t, "x": x, "y": y, "z": -alt, "yaw": 0.0},
            {"time": duration, "x": x + 1.0, "y": y, "z": -alt * 0.95, "yaw": 45.0},
        ]
    return show


async def run_one(n: int, duration: float, idle_floor_ms: float | None = None) -> dict:
    """Flies one complete show at fleet size ``n`` and returns its measurements."""
    print(f"\n=== N={n} ===", flush=True)
    manager = DroneManager(ports=list(range(n)), backend="sim")
    recorder: TickLatencyRecorder | None = None
    phases: dict[str, float] = {}
    result: dict = {"n": n, "show_duration_s": duration}

    with ResourceSampler() as sampler:
        try:
            t0 = time.perf_counter()
            await manager.connect_all()
            phases["connect_s"] = time.perf_counter() - t0

            # FleetState only exists after connect_all(), so the tick recorder
            # is installed here rather than up front.
            fs = manager._sim_fleet.state
            recorder = TickLatencyRecorder(fs, fs.config.dt, idle_floor_ms)
            recorder.install()

            await asyncio.sleep(0.5)  # let telemetry + the armable settle-delay catch up

            t0 = time.perf_counter()
            await manager.launch_sequence()
            phases["launch_s"] = time.perf_counter() - t0

            # Let the armed-state telemetry stream catch up before counting.
            # `_monitor_armed_state` polls at 50ms, so the last drone in the
            # final batch is genuinely armed in the physics for a few tens of ms
            # before `manager.telemetry` reflects it — sampling immediately
            # reports a phantom "1 drone failed to arm" that is really just lag.
            # Both figures are recorded below so a *real* arming failure at
            # scale stays distinguishable from readout lag.
            await asyncio.sleep(0.5)
            armed_after_launch = sum(1 for tel in manager.telemetry.values() if tel["armed"])
            armed_in_physics = int(manager._sim_fleet.state.armed.sum())

            # Tick stats are taken from the show phase only. Launch is
            # dominated by its deliberate 1s stagger — mostly idle waiting,
            # which would dilute the latency numbers toward optimistic.
            recorder.reset()

            t0 = time.perf_counter()
            manager.start_trajectory(benchmark_show(n, duration), mode="LOCAL")
            await asyncio.sleep(duration + 1.5)
            phases["show_s"] = time.perf_counter() - t0

            result["tick"] = recorder.summary()
            result["armed_after_launch"] = armed_after_launch
            result["armed_in_physics"] = armed_in_physics
            result["armed_pct"] = round(100.0 * armed_after_launch / n, 1)
            # A gap here means telemetry is lagging physics — the readout-lag
            # signature, distinct from drones that never armed at all.
            result["telemetry_lag_drones"] = armed_in_physics - armed_after_launch
            result["connected"] = sum(1 for tel in manager.telemetry.values() if tel["connected"])
            result["monitor_tasks"] = len(manager._tasks)
            result["proximity_breaches"] = sum(1 for v in manager.proximity_breaches.values() if v)
            result["geofence_breaches"] = sum(1 for v in manager.geofence_breaches.values() if v)
        finally:
            if recorder is not None:
                recorder.remove()
            t0 = time.perf_counter()
            await manager.shutdown()
            phases["shutdown_s"] = time.perf_counter() - t0

    result["phases_s"] = {k: round(v, 2) for k, v in phases.items()}
    result["wall_clock_s"] = round(sum(phases.values()), 2)
    result["resources"] = sampler.summary()

    t = result["tick"]
    r = result["resources"]
    print(
        f"  wall {result['wall_clock_s']}s "
        f"(connect {phases['connect_s']:.1f} / launch {phases['launch_s']:.1f} / show {phases['show_s']:.1f})",
        flush=True,
    )
    print(
        f"  tick: {t['gap_ms_mean']}ms mean gap "
        f"(target {t['target_dt_ms']}ms, host floor {t['idle_floor_ms']}ms) "
        f"-> +{t['overshoot_ms_mean']}ms overshoot, p95 {t['gap_ms_p95']}, max {t['gap_ms_max']}",
        flush=True,
    )
    print(
        f"        {t['late_tick_pct']}% late, {t['effective_tick_hz']}Hz effective, "
        f"{t['realtime_ratio']}x realtime",
        flush=True,
    )
    print(
        f"  cpu {r['cpu_percent_mean']}% mean / {r['cpu_percent_peak']}% peak, "
        f"rss {r['rss_mb_peak']}MB peak, {result['monitor_tasks']} tasks",
        flush=True,
    )
    print(
        f"  armed {result['armed_after_launch']}/{n} ({result['armed_pct']}%) "
        f"[physics {result['armed_in_physics']}/{n}, "
        f"{result['telemetry_lag_drones']} lagging readout], "
        f"breaches: {result['proximity_breaches']} proximity / {result['geofence_breaches']} geofence",
        flush=True,
    )
    return result


def print_table(results: list[dict]) -> None:
    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)
    header = (
        f"{'N':>6} {'wall_s':>8} {'launch_s':>9} {'show_s':>8} "
        f"{'tick_ms':>8} {'over_ms':>8} {'max':>8} {'late%':>7} {'xRT':>6} "
        f"{'cpu%':>7} {'rss_mb':>8} {'armed%':>7}"
    )
    print(header)
    print("-" * 100)
    for r in results:
        t, res, p = r["tick"], r["resources"], r["phases_s"]
        print(
            f"{r['n']:>6} {r['wall_clock_s']:>8.1f} {p['launch_s']:>9.1f} {p['show_s']:>8.1f} "
            f"{t['gap_ms_mean']:>8.2f} {t['overshoot_ms_mean']:>8.2f} {t['gap_ms_max']:>8.1f} "
            f"{t['late_tick_pct']:>7.1f} {t['realtime_ratio']:>6.2f} "
            f"{res['cpu_percent_mean']:>7.1f} {res['rss_mb_peak']:>8.1f} {r['armed_pct']:>7.1f}"
        )
    print("-" * 100)
    floor = results[0]["tick"]["idle_floor_ms"] if results else 0.0
    print(f"tick_ms = mean tick-to-tick gap (target {results[0]['tick']['target_dt_ms'] if results else 20.0}ms "
          f"at 50Hz; this host's idle floor is {floor}ms).")
    print("over_ms = mean gap past that floor: the part attributable to fleet size.")
    print("late%   = ticks arriving more than 1.5x the floor after the previous one.")
    print("xRT     = simulated seconds per wall-clock second; below 1.0 the fleet flies in slow motion.")
    print("launch_s is deliberate launch stagger (ceil(n/5)*4 s), not load.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "-n", "--sizes", default=",".join(str(s) for s in DEFAULT_SIZES),
        help=f"comma-separated fleet sizes (default: {','.join(str(s) for s in DEFAULT_SIZES)})",
    )
    parser.add_argument(
        "--show-duration", type=float, default=DEFAULT_SHOW_DURATION_S,
        help=f"trajectory duration in seconds (default: {DEFAULT_SHOW_DURATION_S})",
    )
    parser.add_argument(
        "-o", "--output", default=str(Path(__file__).resolve().parent / "benchmark_results.json"),
        help="where to write the JSON results",
    )
    args = parser.parse_args()

    sizes = [int(s.strip()) for s in args.sizes.split(",") if s.strip()]

    print("=== tara-sim fleet-scale benchmark ===")
    print(f"sizes={sizes} show_duration={args.show_duration}s")
    print(f"Python {platform.python_version()} on {platform.platform()}")
    print(f"{psutil.cpu_count(logical=True)} logical cores, "
          f"{psutil.virtual_memory().total / (1024 ** 3):.1f} GB RAM")

    # Calibrate the host's timer granularity once, before any fleet exists, so
    # every result below can be scored against what this machine can actually do.
    default_dt = SimConfig().dt
    idle_floor_ms = asyncio.run(measure_idle_floor(default_dt))
    print(
        f"idle tick floor: {idle_floor_ms:.2f}ms for a {default_dt * 1000:.1f}ms sleep "
        f"(empty event loop, no fleet)"
    )
    if idle_floor_ms > default_dt * 1000.0 * 1.2:
        print(
            f"  NOTE: this host cannot hit {default_dt * 1000:.1f}ms sleeps at all "
            f"(coarse OS timer). Overshoot below is measured against "
            f"{idle_floor_ms:.1f}ms, not {default_dt * 1000:.1f}ms."
        )

    results = []
    for n in sizes:
        results.append(asyncio.run(run_one(n, args.show_duration, idle_floor_ms)))

    print_table(results)

    out = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "logical_cores": psutil.cpu_count(logical=True),
            "physical_cores": psutil.cpu_count(logical=False),
            "total_ram_gb": round(psutil.virtual_memory().total / (1024 ** 3), 1),
            "idle_tick_floor_ms": round(idle_floor_ms, 2),
        },
        "show_duration_s": args.show_duration,
        "results": results,
    }
    out_path = Path(args.output)
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\nResults written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
