# tara-sim fleet-scale benchmark: where it actually stops

Measured with `tools/benchmark_fleet_scale.py`. Reproduce with:

```powershell
.venv\Scripts\python tools\benchmark_fleet_scale.py -n 100,500,1000
```

## The short answer

**The real ceiling is ~500 drones, not 1000.**

`MAX_FLEET_SIZE = 1000` is not reachable at full fidelity on this hardware. The
fleet still *flies* at 1000 — nothing crashes, no safety violations, every drone
arms, memory is irrelevant — but the physics loop stops keeping up with
wall-clock (0.19x real time, 91% of ticks late, CPU pinned at ~93%), so what you
are watching is no longer a real-time show.

500 is the last size that holds the host's tick floor (3% late ticks). By 700
the loop is already missing a quarter of its ticks, and past 800 it collapses.

**What breaks first is not the vectorized physics.** It is the event loop, which
saturates one CPU core. The largest single contributor is the ~8 asyncio
telemetry tasks per drone that `DroneManager` spawns regardless of backend — at
1000 drones, 8000 always-on tasks — but those alone do not account for it; the
trajectory loop competing with them is what pulls the ceiling down (see
[contention](#why-its-the-task-count-not-the-physics) below).

The vectorized design's core claim holds up completely: per-drone physics cost
*falls* as the fleet grows (1.64 µs/drone at N=100 → 0.53 µs/drone at N=5000).
The design is sound. The per-drone asyncio layer around it is what does not
scale.

## End-to-end results

Full runs through the real `DroneManager` on `backend="sim"` — connect, launch,
then a 15 s show — with tick latency sampled during the show phase only.

| N | tasks | tick gap | overshoot | late | xRT | CPU | peak RSS | armed | breaches |
|-----:|------:|------:|------:|-----:|-----:|-----:|-----:|-----:|-----:|
| 100 | 803 | 30.8 ms | +0.7 ms | 0.9% | 0.65 | 20.3% | 93.9 MB | 100/100 | 0 |
| 500 | 4,003 | 31.9 ms | +2.9 ms | 3.3% | 0.63 | 53.6% | 111.0 MB | 500/500 | 0 |
| 700 | 5,603 | 44.2 ms | +14.8 ms | 27.4% | 0.45 | 70.5% | 117.3 MB | 700/700 | 0 |
| 800 | 6,403 | 43.2 ms | +13.8 ms | 30.4% | 0.46 | 80.9% | 121.7 MB | 800/800 | 0 |
| 1000 | 8,003 | 107.7 ms | +75.6 ms | 91.4% | 0.19 | 93.0% | 129.5 MB | 1000/1000 | 0 |

(700 and 800 were run separately to locate the knee; that run measured a
31.07 ms idle floor versus 32.07 ms for the others — normal host variance, and
overshoot is scored against each run's own floor.)

CPU climbs almost linearly with fleet size — 20% → 54% → 70% → 81% → 93% — and
that is the whole story. asyncio is single-threaded, so ~100% of *one* core is
the wall no matter how many the machine has (this one has 8). Tick latency
tracks that curve with a threshold: negligible while CPU has headroom, visible
once it passes ~70% (N=700, 27% late), and collapsing as it approaches the
ceiling (N=1000, 91% late, overshoot 26x worse than at N=500).

The useful boundary is therefore between **500 and 700**: 500 still sits at the
host's floor, 700 has already lost a quarter of its ticks.

The `xRT` column is the practical consequence. `FleetState.tick(dt)` always
advances simulated time by a fixed `dt` no matter how long the loop actually
took, so a loop running at 107 ms per 20 ms tick means the fleet flies at
**0.19x real time** — a 60-second show takes over five minutes and every
timing-derived reading is wrong by 5x.

Note that even N=100 and N=500 show xRT ≈ 0.64. That is the Windows timer
floor, not load (see below) — the fleet is keeping up with everything the host
can offer, it just cannot offer 50 Hz. The 0.19 at N=1000 is genuine
saturation, and it is the number that matters.

Correctness never degraded: every drone armed at every size, with zero
proximity and zero geofence violations throughout.

## Measured on

Windows 11, Python 3.13.2, 8 logical cores (4 physical), 13.9 GB RAM.

One platform note that matters for reading every number below: Windows' default
timer granularity is ~15.6 ms, so `asyncio.sleep(0.02)` returns after ~31 ms
even on a **completely empty** event loop. The 50 Hz `tick_hz` default is
therefore unreachable on this host at *any* fleet size, including zero.

The benchmark measures that idle floor before touching a fleet and scores
everything against it, so the reported overshoot means "cost attributable to
fleet size" rather than "cost of the OS clock". Scoring against the nominal
20 ms instead would report ~11 ms of phantom lateness at every size and make
even a 20-drone fleet look saturated. On Linux, where the floor is ~1 ms, the
absolute gaps will be lower but the *shape* of the degradation below should
be the same.

## Telemetry tasks alone, with nothing else running

To attribute the cost, the same per-drone monitor-task load was also measured
*in isolation* — 8 streaming tasks per drone, no trajectory playback, no
setpoint fan-out — against a 31.2 ms idle floor:

| N | monitor tasks | mean tick gap | p95 | vs. floor |
|------:|------:|------:|------:|------:|
| 500 | 4,000 | 32.1 ms | 41.7 ms | 1.03x |
| 600 | 4,800 | 31.0 ms | 41.9 ms | 0.99x |
| 700 | 5,600 | 32.3 ms | 49.4 ms | 1.03x |
| 800 | 6,400 | 35.4 ms | 57.7 ms | 1.13x |
| 900 | 7,200 | 39.2 ms | 66.3 ms | 1.26x |
| 1000 | 8,000 | 45.0 ms | 79.6 ms | 1.44x |

The telemetry tasks by themselves stay at the floor through N=700 and are only
44% over it at N=1000. **This is much better than the end-to-end numbers above**,
where N=700 is already 27% late and N=1000 is 91% late.

That gap is the finding, not a discrepancy: the telemetry layer is the largest
single contributor, but it is not sufficient on its own to explain the ceiling.
Real degradation starts ~200 drones earlier than this table predicts, because
during an actual show the telemetry tasks compete with the trajectory loop
rather than running alone. Anyone re-testing this should trust the end-to-end
table, not this one.

## Why it's the task count, not the physics

Each component measured on its own, per tick, against its own budget:

| Component | N=100 | N=500 | N=1000 | Budget | Verdict |
|---|---:|---:|---:|---:|---|
| Vectorized physics `tick()` | 0.16 ms | 0.37 ms | 0.64 ms | 20 ms | not the problem |
| Proximity `cKDTree` @10Hz | 0.08 ms | 0.35 ms | 0.91 ms | 100 ms | not the problem |
| Setpoint fan-out @10Hz | 1.9 ms | 6.8 ms | 14.6 ms | 100 ms | contributes |
| **Per-drone monitor tasks** | **800** | **4,000** | **8,000** | — | **the ceiling** |

The physics is the *cheapest* part of the system. At N=1000 it uses 0.64 ms of
a 20 ms budget — 3%. Pushed further in isolation, it ticks 5000 drones in
2.7 ms, still comfortably real-time. If the asyncio layer were free, the numpy
core would carry several thousand drones.

Memory never enters the picture: ~12–27 KB per drone, ~12 MB for a full
1000-drone fleet.

The setpoint fan-out at N=1000 (14.6 ms mean, 55 ms worst-case, against the
100 ms trajectory budget) is the one secondary cost worth naming. On its own it
is not binding, and it grows only linearly.

**But the components do not simply add.** Measured in isolation the telemetry
tasks cost ~+13 ms at N=1000, and the fan-out ~15 ms per trajectory tick — yet
the end-to-end run shows **+75.6 ms**. The excess is contention, not arithmetic:
as CPU approaches saturation, every one of the 8000 telemetry tasks that wakes
up delays the fan-out, the fan-out's 1000 coroutines delay the physics tick, and
the tick's own lateness lets more telemetry work pile up behind it. Below
saturation these costs overlap harmlessly on an idle core; above it they
compound.

This is why component-level measurement alone would have got the ceiling wrong
by about 200 drones — the isolated telemetry table says N=700 is free, and
end-to-end it is already dropping a quarter of its ticks. Only the full run
captures the interaction, which is why the benchmark drives the real
`DroneManager` rather than timing parts.

## What this means in practice

- **N=100** (the existing milestone) is comfortable — the fleet is essentially
  free, and `test_sim_backend.py`'s 100-drone test is nowhere near a limit.
- **N=500** is fine, and is the recommended working ceiling. Sits at the host's
  floor with 3% late ticks — no meaningful degradation.
- **N=700–800** is past the edge: ~27–30% of ticks late at 0.45x real time.
  Still correct, but no longer a faithful real-time preview.
- **N=1000** runs and stays *correct* — zero proximity or geofence violations,
  all 1000 drones armed — but at **0.19x real time** with CPU pinned near 93%.
  Usable for offline validation of a show's geometry, where only the final
  positions matter; actively misleading for anything timing-sensitive, and not
  a demo you would want to run in front of someone.

Correctness holds at every size tested. This is a performance ceiling, not a
safety one.

## Follow-ups (not fixed here — measurement only, per the issue's scope)

1. **The real one:** `DroneManager` spawns 8 telemetry tasks per drone even on
   `backend="sim"`, where all 8 read from the same shared numpy arrays. For the
   sim backend these could be one fleet-wide task doing a vectorized read into
   all telemetry dicts, which would remove the ceiling almost entirely. Note
   this is a `DroneManager` change, not a `tara-sim` one — the simulator is
   already doing the right thing.
2. Setpoint fan-out builds `n` coroutines per trajectory tick; a batched
   `command_setpoint`-style vectorized write would cut the next bottleneck
   before it binds.
3. `MAX_FLEET_SIZE = 1000` in `tara_core/layout.py` advertises a size that is
   not real-time achievable — measured 2x off on this host. Either lower it or
   document it explicitly as an offline-only bound. Note it cannot simply be
   lowered: `_pad_offset` indexes `launch_grid(MAX_FLEET_SIZE)`, whose column
   count is `ceil(sqrt(MAX_FLEET_SIZE))`. Dropping 1000 → 500 narrows the grid
   from 32 columns to 23 and moves **477 of the first 500 drones' pads** — the
   exact "a drone's pad moves as the fleet grows" trap `_pad_offset`'s docstring
   warns about. Documenting the bound is the cheap option; changing it is a
   physical-layout change.

## A note on the numbers

`launch_sequence` stagger is excluded from all latency figures, and dominates
wall-clock everywhere — at N=1000 it is 813 s of the 831 s total. Tick latency
is therefore sampled during the show phase only.

Its cost is **`ceil(n/5) x 4` seconds**, which is worth stating precisely
because the obvious reading of the code gets it wrong. `_launch_one` sleeps
`stagger_index * 1.0`, and a batch of `LAUNCH_BATCH_SIZE = 5` is launched under
one `asyncio.gather` — so the five drones sleep 0/1/2/3/4 s *concurrently* and
the batch takes 4 s, not 5 s and not 1 s. That model matches measurement within
1.7% at every size tested (80.2 s vs. 80 predicted at N=100; 813.5 s vs. 800 at
N=1000).

The ratio is a flat ~4.0x of `ceil(n/5)` at *every* size, including N=100 where
CPU sits at 20% — so this is fixed deliberate waiting, not the event-loop
saturation described above leaking into the launch phase. Worth noting only
because it makes a full `-n 100,500,1000` sweep take ~23 minutes, almost
entirely spent asleep.
