# Why `tara_sim` falls behind real time on the Diwali shows

Investigation under #122. Tools only: nothing in `packages/` was changed, and no fix is
proposed as code here. Fixes are listed at the end for Harsh to approve first.

**Question.** PR #212's `tools/diwali_sim_scale.py` measures a real-time factor (RTF) of 0.98 / 0.93 /
0.78 at 35 / 100 / 500 drones on the Diwali storyboards, while `tools/benchmark_fleet_scale.py` holds
1.00 at the same sizes. Where does the wall-clock time go?

## Answer

**The show-state snapshot, not the physics.** `DroneManager._state_snapshot_loop` re-serialises the
whole active trajectory with `json.dumps` on the event loop every 5 s
(`state_store.save_show_state`). A Diwali show is 73k waypoints at 35 drones, 353k at 100 and 1.08M at
500; the benchmark's show is 4 waypoints per drone. Each snapshot holds the loop for 0.2 / 0.4 / 0.9 s,
a physics tick that has fallen more than 0.1 s behind drops its backlog (`MAX_TICK_BACKLOG_S`), and the
dropped time is the RTF loss.

| | 35 drones | 100 drones | 500 drones |
|---|---:|---:|---:|
| RTF measured here | 0.963 | 0.929 | 0.844 |
| RTF in PR #212 | 0.979 | 0.927 | 0.779 |
| wall-clock lost in the show window | 7.76 s | 25.28 s | 34.04 s |
| time inside `_write_state_snapshot` | 7.91 s (40 stalls, max 282 ms) | 25.20 s (65, max 635 ms) | 32.20 s (37, max 1,320 ms) |
| **RTF with the snapshot write disabled** | n/a | **0.9965** | **0.9895** |

Two controls on the synthetic benchmark show at 500 drones rule out the formations, the launch and the
tool itself:

| 500 drones, synthetic show | waypoints/drone | show | snapshot cost | RTF |
|---|---:|---:|---|---:|
| stock `benchmark_show` | 4 | 15 s | 3 writes, 23 ms each | 1.004 |
| same path, resampled at 10 Hz | 2,161 | 216 s | 36 writes, 33.5 s total, max 1.42 s | **0.837** |

The tick itself is identical in both (11.9 vs 13.1 ms). The only thing that changed is the size of the
trajectory the snapshot has to dump.

## Top 5 costs at 500 drones

Event-loop time in the 218.6 s show window of `diwali_500.json` (`--profiler steps`). "Loop" = time the
event loop spent running that owner's callbacks; the loop was 86.5% busy in total.

| # | Cost | Seconds | % of wall | Detail |
|---|---|---:|---:|---|
| 1 | Onboard mission executor, `FleetState._apply_mission` | 107.7 | 49.3% | 11.67 ms of the 13.07 ms mean tick (89%). A Python `for` over the 500 flying drones doing a `searchsorted` and 6 array writes each. cProfile: 100.0 s cumulative, 75.6 s self, 23.1 s in 4.6M `searchsorted` calls |
| 2 | Show-state snapshot, `_state_snapshot_loop` | 32.2 | 14.7% | 37 stalls, mean 0.87 s, max 1.32 s, one every 5.85 s. **Causes ~32 of the 34.0 s of RTF loss** |
| 3 | Bulk telemetry ingest, `_ingest_bulk_telemetry` | 18.5 | 8.5% | 6.1 ms per call. cProfile: 12.6M `round()` calls = 10.4 s of it |
| 4 | Trajectory watch loop, `_run_trajectory_loop` | 11.4 | 5.2% | 10 Hz `interpolate_waypoint` for all 500 drones to publish the planned point (740k calls, 11.3 s in cProfile) |
| 5 | Remaining numpy physics in `tick` | 9.6 | 4.4% | 1.04 ms per tick |

Below those: proximity monitor 2.7 s (1.2%, cKDTree), geofence monitor 1.3 s (0.6%), everything else
under 1%. GC is not a factor: 6,425 collections in the window cost 0.69 s in total (0.3% of wall), and the
longest single pause is 7.6 ms (25 gen-2 passes, longest 4.3 ms; `gc.freeze()` in `start_trajectory`
keeps the frozen heap out of them).

Cost #1 is the largest cost but is **not** why RTF drops: the tick fits its 20 ms budget at 500 drones
(p99 16.6 ms). It is why there is little room left: with the snapshot out of the way the loop is still
83% busy, and the 500-drone RTF only reaches 0.99, not 1.00 (see "Not explained").

## All sizes

Show window, `--profiler steps`. Seconds are event-loop time; ms are per physics tick.

| | 35 | 100 | 500 |
|---|---:|---:|---:|
| waypoints / drone, show length | 2,096, 209.5 s | 3,526, 352.5 s | 2,165, 216.4 s |
| mean tick cost | 1.59 ms | 3.28 ms | 13.07 ms |
| &nbsp;&nbsp;`_apply_mission` | 0.94 ms (9.5 s) | 2.45 ms (40.3 s) | 11.67 ms (107.7 s) |
| &nbsp;&nbsp;numpy physics | 0.53 ms (5.4 s) | 0.68 ms (11.1 s) | 1.04 ms (9.6 s) |
| snapshot, per write | 0.20 s | 0.39 s | 0.87 s |
| telemetry ingest | 2.2 s | 8.2 s | 18.5 s |
| trajectory watch loop | 1.7 s | 5.5 s | 11.4 s |
| proximity monitor | 0.75 s | 1.85 s | 2.7 s |
| loop busy | 14.9% | 28.2% | 86.5% |
| tick gaps over 100 ms | 41 (9.1 s) | 68 (27.3 s) | 39 (34.2 s) |

At every size the stalled-gap time matches the snapshot time almost exactly, and at 35 and 100 drones
the physics is nowhere near saturated. At 500 drones the median tick gap is 19.98 ms: the loop keeps
time between snapshots, and the RTF is lost in lumps (per-10 s windows run 0.71 to 0.92; none reaches 0.99).

## Method

- `tools/profile_show_rtf.py` flies a show through the same `DroneManager` path as
  `tools/diwali_sim_scale.py` (connect, `upload_show`, `launch_sequence`, `start_trajectory`,
  with the same 5 Hz position sampling on the loop) and measures only the show window.
- `--profiler steps` patches `asyncio.events.Handle._run` in-process and charges each callback's
  duration to the coroutine that owns it; it wraps `FleetState.tick`, its sub-steps, bulk telemetry
  and the snapshot write on the live instances, and adds a `gc.callbacks` hook. Overhead is small:
  500 drones read 0.844 with it and 0.843 under cProfile.
- `--profiler cprofile` is cProfile over the show window. Its `threading.wait` / `lock.acquire` lines
  are the resource-sampler and guard threads, not loop time; they are excluded from the tables above.
- `--disable snapshot` replaces `manager._write_state_snapshot` with a no-op for that process. It is an
  experiment, not a baseline.
- Shows are the `diwali_{35,100,500}.json` exports built from PR #212 at `6d50fb7`.
- **SITL guard.** Every run waited for no `tara-sitl-*` container in `docker ps` and re-checked every
  5 s during the run (a run that overlapped one is written `"valid": false`). The first run waited
  1,585 s for `tara-sitl-gcsfix`; every run below is valid, none overlapped a container.
- Host: Ubuntu 24.04, Python 3.12.3, 16 logical cores, 15 GB.

Reproduce: `tools/profile_show_rtf_matrix.sh <dir with diwali_*.json> [out_dir]` (about 1.5 h, mostly
the 400 s launch of every 500-drone run).

## Not explained, and limits

- **One run per configuration.** The three 500-drone runs that carry the full snapshot load read 0.844
  (steps), 0.843 (cProfile) and 0.837 (dense synthetic), so run-to-run spread is about 0.01 on this host.
- **My RTF is not PR #212's.** 0.844 vs 0.779 at 500, 0.963 vs 0.979 at 35. Running PR #212's own `tools/diwali_sim_scale.py` on `diwali_500.json` on this host gave
  0.851 (max tick gap 1,090 ms), against 0.844 from this harness, so the two tools agree here and the
  difference to the 0.779 in `sim_500.json` is the host or its load at the time, not the tool. I did not
  establish which. (For that one run I checked `docker ps` before and after, not continuously.)
- **The last ~2.3 s of loss at 500 drones** (RTF 0.9895 with the snapshot off) is two one-off stalls in
  `_run_trajectory_loop` in the first 2 s of the show (544 ms and 740 ms) plus a thin tail of ticks
  over 25 ms. I did not find what the first-iteration stall is.
- **`_apply_mission` does not scale linearly.** 26.8 / 24.5 / 23.3 µs per drone at 35 / 100 / 500, but
  the stock synthetic at 1,000 drones measured 12.5 ms per tick (12.5 µs per drone) with all 1,000 armed
  in physics, and RTF 0.998. I did not investigate why, so I am not extrapolating the executor's cost
  past 500 drones. A dense 1,000-drone show was not run; its snapshot would be about twice the 500-drone one.
- The 400 s `launch_s` at 500 drones is the deliberate 1 s stagger (`ceil(n/5) * 4` s) and is outside the
  show window.

## Candidate fixes (not implemented; need Harsh's approval first)

1. **Snapshot** (removes ~32 of 34 lost seconds at 500; measured upper bound above). The trajectory does
   not change while a show runs, yet every snapshot rewrites all of it. Write it once at
   `start_trajectory`, and snapshot only the small playback state (mode, start time, per-drone telemetry)
   every 5 s. A worker thread alone would not help: the C `json.dumps` holds the GIL for the whole call.
   Needs a decision on the resume file format (`load_show_state` / `resume_pending_show` read it).
2. **`_apply_mission`**: vectorise across drones (stack the per-drone waypoint arrays once at
   `load_mission`, one batched lookup per tick). This is headroom, not the cause of the RTF drop.
3. **Telemetry ingest**: the `round()` calls dominate; `np.round` would change half-way cases
   (the code comment says why Python's `round` is kept), so any change needs the same-output check.

The per-run JSON files are not committed; every number cited above comes from them and the matrix
script regenerates them.
