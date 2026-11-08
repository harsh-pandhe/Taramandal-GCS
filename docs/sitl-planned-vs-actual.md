# Studio show on PX4 SITL + Gazebo: planned vs actual (#172, T2)

**Tier:** T2 (PX4 SITL + Gazebo), real flight dynamics. These are simulated
flights, not hardware.
**Date:** 2026-11-01. **Commit flown:** `3ee84a1` (main after #209).
**Tool:** [`tools/sitl_planned_vs_actual.py`](../tools/sitl_planned_vs_actual.py).
**Raw run records:** [`docs/sitl-planned-vs-actual/`](sitl-planned-vs-actual/), one JSON file per run.

**Update 2026-11-03:** the same show was flown after the F3 fix: all 5 runs pass the
tool's rule on `b435358`, see [section E](#e-after-f3-the-real-diwali-segment-on-b435358).
Whether that meets #172's acceptance is Harsh's decision.

**Update 2026-11-02:** F1 and F2 are now fixed on main. The real Diwali 3-drone
segment passes 3 runs out of 3 on `7a6ab70` with no rig workarounds, with the F3
start dive still present: see [section D](#d-baseline-f3-dive-present-the-real-diwali-segment-on-main).
The paragraph below and sections A to C describe the earlier runs on `3ee84a1`.

## Result in one paragraph

The whole software chain works on real PX4 SITL. Studio builds the show and
certifies it (jerk included). Studio signs the export, and the GCS verifies
the signature and uploads a mission to every drone. The pre-arm latch, launch,
formation hold and the `execution="streamed"` start all work, and the show
plays to SHOW_COMPLETE with a clean land and disarm. **But no run counts as
a pass with the repository as it is.** Gazebo spawns the drones along the
wrong axis for the GCS's fleet frame. The GCS then believes drone *i* is
(+2.2·*i* m north, −2.2·*i* m east) from where it really is. Gazebo saw the
drones come within **0.34–0.56 m** of each other when Studio predicted
**2.05 m**. One of those runs was a collision that ended in a geofence RTL.
The GCS proximity monitor never fired for the real near-misses, because in
its frame the drones were 2 m apart. With only the spawn axis corrected (a
rig-only diagnostic, not a repository change), the same chain gives **three
consecutive passes at 3 drones**. Gazebo then saw a minimum separation of
2.02–2.10 m against Studio's 2.05 m, steady tracking error under 0.5 m, and
every drone landed within 8 cm of its pad. Scaling with the corrected spawn
is clean up to 8 drones. It degrades at 9 (Gazebo at 0.84× real time) and
badly at 10 (0.65×, 3.5 m steady tracking error).

## Setup

| | |
|---|---|
| Stack | `./tara up --backend px4 -n N` inside one container (`tara-sitl-172`, image `taramandal-px4-sitl:v1.16.2`). All five services ran: fleet, gcs-api, studio-api, gcs-web, studio-web. The whole stack was restarted for every run. |
| PX4 / Gazebo | PX4 v1.16.2 (`gz_x500`), Gazebo Sim 8.15.0, world `drone_show_field`, headless |
| Machine | Docker Desktop VM: 16 CPUs, 3.9 GB RAM. At 10 drones the container used about 1.0 GB and about 7.8 CPUs, so CPU, not RAM, is the limit. |
| GCS | `mode=SIMULATION`, `backend=px4`, `fidelity=px4-sitl` (from `/api/status`). Signed uploads only (`--allow-unsigned` off). One trusted key in `[security].trusted_keys` (key id `7fa30d7106e1dfbf`, signer "Studio SITL #172"). Default safety profile. |
| Studio | Backend signing key from `STUDIO_SIGNING_KEY`. Show built by Studio's own sequencer (`buildSequencedShow`, one circle scene, 6 s hold, 4 s transition, 10 Hz), the same stand-in the #209 test uses. Tab 4's Diwali 3-drone segment was not ready; re-run with `--show <file>` once it is. |
| Start | `POST /api/start-trajectory {"mode": "LOCAL", "execution": "streamed"}` |
| RViz | **Not run.** The image has no ROS 2, and Docker Desktop's VM has no display path to the host. The recording with Gazebo, RViz and the dashboard visible is still open (owned by Harsh, as with #171). |

### Rig-only workarounds

Neither of these changes the repository.

1. **Mission upload directory.** Inside the container, `mkdir -p
   build/px4_sitl_default/rootfs/<i>/fs/microsd`. Without it, every
   `/api/upload-mission` fails on SITL (finding F2), and the launch gate then
   refuses to arm.
2. **Diagnostic runs only (`enu-*`).** A copy of `sim/px4/launch_fleet.sh`
   with `PX4_GZ_MODEL_POSE="${Y_OFFSET},0,…"` instead of `"0,${Y_OFFSET},…"`,
   bind-mounted over the original inside the container. This is the
   one-line spawn-axis fix finding F1 proposes. It was applied only to test
   the diagnosis.

## How planned vs actual is measured

- **Planned:** Studio `/api/validate`: duration, `metrics.min_distance_m`,
  and the jerk, velocity and altitude metrics. The tool also recomputes the
  planned separation at the same instants Gazebo was sampled, and it matches
  Studio's 2.05 m.
- **Actual (ground truth):** Gazebo's own model poses
  (`/world/drone_show_field/dynamic_pose/info`, about 50 Hz) through
  `gz topic --json-output`. These do not pass through PX4 or the GCS, so a
  GCS frame error shows up instead of cancelling out. Gazebo's ENU world is
  mapped to the fleet frame as x = north = gz y, y = east = gz x. Altitude is
  gz z minus each model's resting z.
- **Tracking error:** the distance between each drone's Gazebo position and
  its planned point at the same show time. The planned point uses the same
  linear interpolation the GCS streams from (it matched
  `tara_core.geometry.interpolate_waypoint` on 6,000 random samples).
  - The *start transient* is the time until the error first drops below
    0.5 m (finding F3).
  - *After transient* is the worst error from then on.
  - A drone that never gets within 0.5 m reports `never`, and its whole show
    counts.
- **The GCS's own view:** its telemetry CSV (`/api/export-telemetry`), compared
  with the plan and with Gazebo. A constant offset between the GCS and Gazebo
  is a frame error, not a tracking error.
- **Verdict:** a PASS needs all of the following:
  - the show was certified, signed and verified, and started streamed;
  - it reached SHOW_COMPLETE with no aborted drone and no RTL, ESTOP or
    LANDING failsafe;
  - Gazebo's minimum separation stayed at or above `min_separation_m`
    (1.5 m, the GCS's own abort limit);
  - every drone was disarmed at the end;
  - the sim was live, with no gap over 2 s (a stalled run is DISCARD, not
    counted).

  A pass with real-time factor < 0.9 is marked *degraded*.

## Results

### A. As shipped: 3 drones, 3 consecutive runs (acceptance attempt)

| Run | Verdict | Studio min sep (m) | Gazebo min sep (m) | Max tracking error (m) | Start transient (s) | Max error after transient (m) | GCS − Gazebo offset, drones 1 / 2 (m, N/E) | RTF | Abort |
|---|---|---|---|---|---|---|---|---|---|
| asis-n3-p1 | FAIL | 2.05 | **0.562** | 8.50 | never (drones 1, 2) | 8.49 | (−2.23, +2.19) / (−4.34, +4.40) | 0.999 | none |
| asis-n3-p2 | FAIL | 2.05 | **0.336** | 22.85 | never (drones 1, 2) | 22.85 | (−2.19, +2.12) / (−4.41, +4.37) | 0.919 | geofence RTL, then proximity RTL |
| asis-n3-p3 | FAIL | 2.05 | **0.460** | 8.59 | never (drones 1, 2) | 8.59 | (−2.16, +2.17) / (−4.39, +4.40) | 0.998 | none |

All three completed the chain up to the streamed start, and runs 1 and 3
played the full 90.2 s show (actual playback 90.30 s and 90.23 s). In run 2,
drones 0 and 1 met at 0.34 m centre to centre (an x500 is about 0.6 m across
its rotors). Drone 0 was thrown 22 m off its path, the GCS geofence fired RTL
for the fleet at t = 72 s, and an operator LAND was later overridden back to
RTL by the geofence. Drone 0, the only drone whose pad offset is zero, tracked
normally: RMS 0.20 m, landed 2 cm from its pad.

### B. Diagnostic: spawn axis corrected, 3 drones, 3 consecutive runs

| Run | Verdict | Planned / actual playback (s) | Studio min sep (m) | Gazebo min sep (m) | Max tracking error (m) | Start transient (s) | Max error after transient (m) | Worst landing error (m) | Worst GCS − Gazebo offset (m) | RTF | Abort |
|---|---|---|---|---|---|---|---|---|---|---|---|
| enu-n3-p1 | PASS | 90.2 / 90.28 | 2.05 | 2.022 | 5.90 (drone 2, t=0.1 s) | 3.69 | 0.456 | 0.075 | 0.07 | 0.996 | none |
| enu-n3-p2 | PASS | 90.2 / 90.28 | 2.05 | 2.097 | 5.95 (drone 2, t=0.1 s) | 3.79 | 0.490 | 0.033 | 0.07 | 0.998 | none |
| enu-n3-p3 | PASS | 90.2 / 90.30 | 2.05 | 2.060 | 5.86 (drone 2, t=0.1 s) | 3.53 | 0.498 | 0.054 | 0.03 | 0.999 | none |

The max tracking error is entirely the start dive (finding F3). From t = 20 s
on, the worst error in any run was 0.39 m. Every run went from launch to
FORMATION_HOLD in about 10.7 s.

### C. Stretch: spawn axis corrected, stepping up the drone count (one run each)

| Drones | Verdict | Planned / actual playback (s) | Launch → FORMATION_HOLD (s) | Studio min sep (m) | Gazebo min sep (m) | Max tracking error (m) | Start transient (s) | Max error after transient (m) | Max error, t ≥ 20 s (m) | Worst landing error (m) | RTF | Abort |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 3 | PASS | 90.2 / 90.28 | 10.7 | 2.05 | 2.022 | 5.90 (drone 2, t=0.1 s) | 3.69 | 0.456 | 0.39 | 0.075 | 0.996 | none |
| 5 | PASS | 101.4 / 101.41 | 13.7 | 2.05 | 2.008 | 9.96 (drone 4, t=0.0 s) | 5.62 | 0.483 | 0.44 | 0.047 | 0.958 | none |
| 7 | PASS | 106.6 / 106.65 | 20.3 | 2.05 | 2.023 | 13.99 (drone 6, t=0.0 s) | 7.29 | 0.491 | 0.44 | 0.064 | 0.966 | none |
| 8 | PASS | 110.4 / 110.49 | 22.3 | 2.05 | 2.036 | 15.93 (drone 7, t=0.1 s) | 7.56 | 0.499 | 0.48 | 0.052 | 0.956 | none |
| 9 | PASS (degraded: RTF 0.841) | 107.0 / 107.03 | 30.9 | 2.05 | 2.006 | 17.88 (drone 8, t=0.0 s) | 8.93 | 0.865 | 0.86 | 0.051 | 0.841 | none |
| 10 | PASS (degraded: RTF 0.648) | 115.4 / 115.41 | 33.6 | 2.05 | **1.974** | 19.94 (drone 9, t=0.0 s) | 9.83 | **3.452** | **3.45** | 0.055 | **0.648** | none |

(The 3-drone row is enu-n3-p1 from section B.) **Where it degrades on this
machine:**
- **Up to 8 drones** it is clean: real-time factor at least 0.956, steady
  error at most 0.48 m.
- **At 9** Gazebo runs at 0.84× real time and the steady error doubles to
  0.86 m.
- **At 10** it runs at 0.65×, the steady error is 3.45 m, and Gazebo
  separation dips below Studio's prediction (1.97 m against 2.05 m). That is
  still above the 1.5 m abort, and nothing aborted.

Separately, the time from launch to formation hold grows from 11 s to 34 s,
because drones launch in batches of 5 and climb to higher layers.

The stand-in show gets longer with N (90.2 s at 3 drones, 115.4 s at 10),
because the sequencer scales its transitions to the formation size. Higher
drones also hover higher before the start (`altitude_layers`: 2, 4, …, 20 m),
so the start transient grows with N regardless of sim speed.

### D. Baseline, F3 dive present: the real Diwali segment on main

**Flown 2026-11-02 on `7a6ab70`.** F1 (#215) and F2 (#217) are merged; the F3
fix (#220) is not, so every run still starts with the dive. This section is the
baseline to compare against once F3 lands; the runs get repeated then.

- **Show (3 drones):** `examples/diwali/segments/diwali_3drone_segment.json`,
  84.2 s. `tools/diwali_run_all.sh` with `DIWALI_SIZES=3` rebuilt it
  byte-identically. The tool then re-exported it with `sign: true`, which left
  the waypoints unchanged, and the GCS verified the signature on every upload.
  `--allow-unsigned` was not used.
- **Show (5 and 8 drones):** the same storyboard built with
  `tools/diwali_build.mjs --n N`. Both certified with 0 violations.
- **No rig workarounds:** no manual `mkdir` and no patched `launch_fleet.sh`.
  Every run used a brand-new container, so the mission upload created
  `/fs/microsd/tara` itself each time (0 `microsd` directories before the stack
  started, all missions verified).
- **Rig setup that remains:** the image has no Node, so the host's Node and npm
  are copied in, and the Python venv and `node_modules` come from the host
  checkout. A scratch `tara.toml` adds the trusted key; it is otherwise main's
  file.

| Run | Drones | Verdict | Studio min sep (m) | Gazebo min sep (m) | Start transient (s) | Max error after transient, tool (m) | Max error from 1 s after transient (m) | Max tracking error, incl. dive (m) | Worst landing error (m) | RTF | Abort / RTL | Final state |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline-f3dive-n3-p1 | 3 | PASS | 2.20 | 2.169 | 3.71 | 0.496 | 0.323 | 5.96 | 0.083 | 0.993 | none | DISARMED |
| baseline-f3dive-n3-p2 | 3 | PASS | 2.20 | 2.159 | 3.64 | 0.497 | 0.337 | 5.98 | 0.044 | 0.997 | none | DISARMED |
| baseline-f3dive-n3-p3 | 3 | PASS | 2.20 | 2.147 | 3.52 | 0.500 | 0.274 | 5.91 | 0.065 | 1.000 | none | DISARMED |
| baseline-f3dive-n5-p1 | 5 | PASS | 2.20 | 2.152 | 5.52 | 0.497 | 0.307 | 9.93 | 0.054 | 0.980 | none | DISARMED |
| baseline-f3dive-n8-p1 | 8 | PASS | 2.20 | 2.106 | 7.67 | 0.643 | 0.641 | 15.91 | 0.048 | 0.913 | none | DISARMED |

All five met the tool's pass rule: certified and signed, SHOW_COMPLETE, no
abort or RTL, Gazebo min separation at or above 1.5 m, all drones disarmed. No
run was discarded and none stalled (longest stall 0.05 s). No drone was
excluded or re-mapped at upload. Landing used the normal operator LAND after
SHOW_COMPLETE.

**Reading the table:**

- **The dive is visible and grows with fleet size.** Each drone drops from its
  formation layer to meet the plan, which is climbing from the pad. That gives
  the 5.9 m maximum error at 3 drones and 15.9 m at 8. The lowest point in the
  first 12 s was 0.76–0.81 m above ground. Peak descent was about 1.6 m/s. It
  did not bring any pair under the limit.
- **The tool's "max error after transient" is close to 0.5 m by construction.**
  It measures from the moment the error first drops under 0.5 m, so it starts
  at that value. The "from 1 s after transient" column is the better figure for
  steady tracking. It is recomputed from the 10 Hz CSVs and stored in each
  JSON's `run_context`.
- **8 drones is near the rig's limit.** Real-time factor (RTF) was 0.913, just
  above the tool's 0.9 "degraded" line, and steady tracking error about doubled
  to 0.64 m. Nothing else was running in Docker, so this comes from fleet size.
  It is one run.
- **Host load did not affect the results.** 1-minute load was 2.8–4.0 before
  each run, and RTF was 0.98 or better at 3 and 5 drones.

Each JSON carries `run_context.note = "baseline, F3 dive present"`.

### E. After F3: the real Diwali segment on `b435358`

**Flown 2026-11-03 on `b435358`**, with #219 and #220 merged and CI green on that
commit (7 of 7 jobs). The shows are byte-identical to section D's, so differences
come from the GCS. `origin/main` was `6f18df0` by then. The only runtime change
between the two is `blackbox.py` creating session files with mode 0600. The show
and this tool are the same on both.

Same setup as section D: signed re-export, no `--allow-unsigned`, no manual `mkdir`,
no patched launcher, and a new container per run. No other containers were running,
and 1-minute host load was under 4 before each run. No run was discarded.

| Run | Commit | Drones | Tool verdict | Per-drone check | Studio min sep (m) | Gazebo min sep (m) | Start transient (s) | Max tracking error (m) | Worst landing error (m) | RTF | Start leg (s) | Peak descent (m/s) | Lowest alt. in start leg (m) | Abort / RTL | Final state | Host load before (1 / 5 / 15 min) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| postf3-n3-p1 | `b435358` | 3 | PASS | ok | 2.20 | 2.119 | 0.00 | 0.279 | 0.216 | 0.999 | 8.0 | 1.38 | 0.00 | none | DISARMED | 1.17 / 0.76 / 0.58 |
| postf3-n3-p2 | `b435358` | 3 | PASS | ok | 2.20 | 2.138 | 0.00 | 0.308 | 0.066 | 0.999 | 8.0 | 1.39 | 0.00 | none | DISARMED | 2.06 / 3.84 / 2.18 |
| postf3-n3-p3 | `b435358` | 3 | PASS | ok | 2.20 | 2.167 | 0.01 | 0.322 | 0.031 | 0.999 | 8.0 | 1.37 | 0.00 | none | DISARMED | 1.47 / 4.32 / 3.03 |
| postf3-n5-p1 | `b435358` | 5 | PASS (degraded: real-time factor 0.885) | ok | 2.20 | 2.123 | 0.00 | 0.532 | 0.066 | 0.885 | 13.3 | 1.48 | 0.00 | none | DISARMED | 1.78 / 4.43 / 3.54 |
| postf3-n8-p1 | `b435358` | 8 | PASS | ok | 2.20 | 2.113 | 0.01 | 0.533 | 0.046 | 0.934 | 21.4 | 1.53 | 0.00 | none | DISARMED | 1.61 / 5.52 / 4.62 |

Peak descent is measured over 0.25 s. Each JSON carries
`run_context.note = "post-F3 (#219, #220 merged)"`.

**Before and after F3 (section D, then this section):**

| | 3 drones, D | 3 drones, E | 5 drones, D | 5 drones, E | 8 drones, D | 8 drones, E |
|---|---|---|---|---|---|---|
| Max tracking error (m) | 5.91–5.98 | 0.279–0.322 | 9.93 | 0.532 | 15.91 | 0.533 |
| Worst landing error (m) | 0.083 | 0.216 | 0.054 | 0.066 | 0.048 | 0.046 |
| Lowest altitude at the start (m) | 0.76–0.80 | 0.00 | 0.76 | 0.00 | 0.81 | 0.00 |
| RTF | 0.993–1.000 | 0.999 | 0.980 | 0.885 | 0.913 | 0.934 |

The two tracking rows are not the same measurement. D compares against the show
alone, so it includes the dive. E compares against the start leg and then the show.
The tool's "start transient" (under 0.02 s here) should be read the same way.

**Reading the results:**

- **The tracking error from the dive is gone.** The worst error over the whole flight
  is now 0.28–0.32 m at 3 drones and 0.53 m at 5 and 8.
- **The drones still go down to the pad.** The start leg is smooth (peak descent
  1.37–1.53 m/s), but it ends at the show's first waypoint, at pad height, so
  altitude reaches 0.00 m before the show climbs. Before F3 the low point was about
  0.8 m. The leg also delays the show: 8.0 s at 3 drones, 13.3 s at 5, 21.4 s at 8.
  PX4 did not treat this as a landing: each drone logged one takeoff and one landing,
  with no failsafe.
- **The 5-drone run is flagged degraded.** RTF 0.885 is under the tool's 0.9 line,
  and it stayed at 0.86–0.90 for the whole flight, lower than the 8-drone run. The
  host process list at the end of the run shows no other heavy job, so the cause is
  unknown. Nothing stalled (longest 0.03 s) and tracking stayed at 0.53 m. It is one
  run and was not rerun.
- **Worst landing error is one event.** Drone 2 in `postf3-n3-p1` shifted about 0.2 m
  sideways in its final landing and ended 0.216 m from its planned point. Every other
  drone in these runs ended within 0.07 m.
- **RTF falls after the show.** Once the drones are on the ground it drops as low as
  0.43 at 8 drones. That is outside the scored window.

**Per-drone check.** The tool's PASS does not check that every drone flew: `rb2-n5-r1`
on #172 scored PASS with drone 3 still on its pad. So each run here also had a check
outside the tool: OFFBOARD seen, peak altitude within 1 m of the planned peak, and
maximum tracking error under 2 m. All drones in all 5 runs pass, and the result is in
`run_context.per_drone_flight_check`. The drone-3 event did not recur, but 5 clean
runs do not show it is gone. The tool's verdict is unchanged, because this is a
docs-only change.

**Records.** The JSON files are committed here. CSVs, signed shows, PX4 ulogs and
console logs, and GCS black-box files are kept on the dev machine, in
`~/Desktop/tara-sitl-scratch-172c`. An earlier 3-drone run on the same commit also
passed, but it has no PX4 logs, so it is not counted or committed.

### F. Drone that armed but never took off: 10 repeats at 5 drones

**Flown 2026-11-04 on `d7e7b2e`.** On #172, run `rb2-n5-r1` (on `b435358`) scored PASS
although drone 3 armed, stayed in HOLD, never climbed and disarmed 12.9 s later. That
run kept no PX4 log. These repeats look for the event with PX4's own records kept.

- **Show:** the 5-drone Diwali segment (`storyboard_3.json` built at 5 drones), with
  waypoints identical to `rb2-n5-r1`'s show.
- **Setup:** a new container per repeat, signed export, no rig workarounds, 20 s
  settle once all drones are connected.
- **Kept per repeat:** PX4 ulogs and console logs, GCS black-box, GCS log, and the
  Docker VM's memory and swap sampled every 2 s.
- **Rules:** a repeat is not counted if the VM swaps or the sim stalls. Each drone
  passes the per-drone check from section E: OFFBOARD seen, peak within 1 m of
  planned, and maximum tracking error under 2 m.

**Not reproduced in 10.** All 10 valid repeats are CLEAN, and every drone flew the
show. At the original rate of 1 in 7, there is a 21% chance ((6/7)^10) of seeing no
failure in 10 runs, so this does not show the problem is fixed. `main` also differs
from `b435358` by the GCS changes #231–#233.

| Run | Verdict | Drone 0 | Drone 1 | Drone 2 | Drone 3 | Drone 4 | Tool verdict | RTF | Gazebo min sep (m) | Max tracking error (m) | Min VM memory free (MiB) | VM swap-out (pages) | Host load before (1 / 5 / 15 min) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| d3-n5-r01 | INVALID: VM swapped during stack start (pswpout +104151 pages) | not flown (run stopped) | not flown (run stopped) | not flown (run stopped) | not flown (run stopped) | not flown (run stopped) | not run | — | — | — | 907 | 104151 | 2.69 / 5.82 / 6.85 |
| d3-n5-r02 | CLEAN | flew | flew | flew | flew | flew | PASS | 0.933 | 2.159 | 0.459 | 2074 | 0 | 2.65 / 4.91 / 6.34 |
| d3-n5-r03 | CLEAN | flew | flew | flew | flew | flew | PASS | 0.917 | 2.13 | 0.422 | 2097 | 0 | 2.72 / 5.51 / 6.40 |
| d3-n5-r04 | CLEAN | flew | flew | flew | flew | flew | PASS | 0.913 | 2.104 | 0.509 | 2150 | 0 | 1.84 / 5.75 / 6.50 |
| d3-n5-r05 | CLEAN | flew | flew | flew | flew | flew | PASS | 0.922 | 2.102 | 0.452 | 2053 | 0 | 2.04 / 6.19 / 6.79 |
| d3-n5-r06 | CLEAN | flew | flew | flew | flew | flew | PASS | 0.92 | 2.124 | 0.479 | 2083 | 0 | 2.06 / 5.95 / 6.77 |
| d3-n5-r07 | CLEAN | flew | flew | flew | flew | flew | PASS | 0.926 | 2.126 | 0.433 | 2076 | 0 | 1.87 / 5.95 / 6.78 |
| d3-n5-r08 | CLEAN | flew | flew | flew | flew | flew | PASS | 0.914 | 2.105 | 0.474 | 2093 | 0 | 2.33 / 6.33 / 6.98 |
| d3-n5-r09 | CLEAN | flew | flew | flew | flew | flew | PASS | 0.906 | 2.138 | 0.509 | 2109 | 0 | 1.86 / 6.02 / 6.90 |
| d3-n5-r10 | CLEAN | flew | flew | flew | flew | flew | PASS | 0.908 | 2.122 | 0.487 | 2093 | 0 | 2.06 / 6.13 / 6.98 |
| d3-n5-r11 | CLEAN | flew | flew | flew | flew | flew | PASS | 0.905 | 2.161 | 0.442 | 2110 | 0 | 1.79 / 6.08 / 6.99 |

`d3-n5-r01` was stopped before the flight: the VM swapped about 400 MiB while the
stack started, with free memory falling to 907 MiB. It is not counted, and `r11`
replaces it. In the 10 valid repeats free memory never fell below 2053 MiB, and
nothing swapped.

| PX4's record (ulog), each of the 50 drone-flights | Result |
|---|---|
| Arm command (`COMPONENT_ARM_DISARM`) | 50 of 50 ACCEPTED |
| OFFBOARD request (`DO_SET_MODE`, custom mode 6) | exactly 1 per drone, 50 of 50 ACCEPTED |
| Arm to OFFBOARD (PX4 clock) | 0.024–0.036 s |
| Mode changes | identical for all 50: DISARMED/HOLD → ARMED/HOLD → ARMED/OFFBOARD → ARMED/HOLD → ARMED/LAND → DISARMED |
| Disarm reason | 50 of 50 `auto_disarm_land` |
| Failsafe, WARN/ERR after arming | none; the only WARN is "Preflight Fail: No connection to the ground control station" at boot, before the GCS connects |
| Key parameters in force (all drones, all runs) | `COM_DISARM_PRFLT` 10 s, `COM_DISARM_LAND` 2 s, `COM_OF_LOSS_T` 1 s, `COM_OBL_RC_ACT` 0, `COM_RC_IN_MODE` 1, `NAV_RCL_ACT` 2, `NAV_DLL_ACT` 2, `COM_DL_LOSS_T` 10 s, `COM_ARM_WO_GPS` 1, `MPC_TKO_SPEED` 1.5, `MPC_Z_VEL_MAX_DN` 1.5, `SDLOG_MODE` 1 |

Because nothing failed, there is no PX4 failing event to report. `rb2-n5-r1`'s
drone 3 sat armed in HOLD for 12.9 s, then disarmed. That fits PX4's preflight
auto-disarm (`COM_DISARM_PRFLT` = 10 s) after an OFFBOARD switch that never
happened. It is not verified: that run kept no PX4 log, so a refused OFFBOARD cannot
be ruled out.

- **Staggered arming:** this happened in every repeat, with drone 3 arming 2.0–4.1 s
  after drone 0 (2.8 s in `rb2-n5-r1`), so a late arm alone does not trigger it.
- **Duplicate acks:** PX4 acknowledges every `DO_SET_MODE` twice. That is the source
  of the GCS log's "Received ack for not-existing command: 176" (10 per run, in every
  run, clean or not).

**Records:** `docs/sitl-planned-vs-actual/drone3-repeats/` holds one
`<run>.investigation.json` per repeat, with:
- per-drone verdict and black-box mode/armed timeline;
- PX4 commands, acks, mode changes and messages, with key parameters per drone;
- memory and host load.

The same folder has the tool record per flown repeat, and the full PX4 parameter set
in force, from one drone. The ulogs, console logs and black-box files are kept on the
dev machine.

### G. Final regression on `f556537` (main, 7 of 7 CI green)

**Flown on `f556537`: the first 5-drone run at 19:29 UTC on 2026-11-08 (00:59 IST on 2026-11-09), the rerun at 20:24 UTC (01:54 IST)**, with #244
(PX4-safe pacing) merged and CI green on that commit. The 3-drone show is the
committed `diwali_3drone_segment.json` (the rebuild on this commit is not
byte-identical to it, so the committed file was flown). The 5-drone show was rebuilt
on this commit and **differs from the one flown in sections E and F** because of
#244's pacing. **Both 5-drone runs below flew the same 5-drone show file.**

Same setup as sections D and E: signed re-export, no `--allow-unsigned`, a new
container per run, no other `tara-sitl-*` container running, and 1-minute host load
under 4 before each run. The rule for PASS was fixed in advance: the tool passes,
every drone passes the per-drone check (OFFBOARD seen, peak altitude within 1 m of
plan, tracking under 2 m), PX4 ulogs and console logs exist for every drone, and
`launch_failures` stays empty in every 2 s `/api/status` sample.

| Run | Counted | Drones | Tool verdict | Per-drone check | `launch_failures` | ulogs + console logs | Studio min sep (m) | Gazebo min sep (m) | Max tracking error (m) | Worst landing error (m) | RTF | Longest stall (s) | Start leg (s) | Emergency transitions | Final state | Host load before (1 / 5 / 15 min) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| final-n3-p1 | no (VM swapped during stack start) | 3 | not flown | n/a | empty | all 3 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 2.55 / 2.20 / 1.21 |
| final-n3-p1-retry1 | yes (p1 rerun) | 3 | PASS | ok | empty | all 3 | 2.20 | 2.122 | 0.332 | 0.070 | 0.999 | 0.05 | 8.0 | 0 | DISARMED | 2.20 / 2.30 / 1.35 |
| final-n3-p2 | yes | 3 | PASS | ok | empty | all 3 | 2.20 | 2.114 | 0.519 | 0.041 | 1.000 | 0.03 | 8.0 | 0 | DISARMED | 1.84 / 2.74 / 1.78 |
| final-n3-p3 | yes | 3 | PASS | ok | empty | all 3 | 2.20 | 2.154 | 0.274 | 0.062 | 1.000 | 0.02 | 8.0 | 0 | DISARMED | 1.13 / 2.27 / 1.83 |
| final-n5-p1 | yes | 5 | **FAIL: emergency transition; final state RTL** | **FAIL (5 of 5)** | empty | all 5 | 2.20 | 2.142 | **11.97** | 32.893 | **0.575** | 0.19 | 13.4 | 3 | RTL | 2.65 / 2.65 / 2.08 |
| final-n5-p2 (rerun, same show) | yes | 5 | **PASS** | ok | empty | all 5 | 2.20 | 2.150 | 0.326 | 0.085 | **0.998** | 0.03 | 13.4 | 0 | DISARMED | **2.91 / 15.99 / 51.35** |

**The 3-drone half passes: three consecutive runs, each in a fresh container.** The
first attempt (`final-n3-p1`) was voided because the VM swapped while the stack was
starting (108,371 pages out) and the drones never flew. It was rerun and not counted.
Memory and swap inside the Docker VM were clean in all counted runs (minimum available
1,947 MiB, 0 swap-out pages). `launch_failures` was empty in every sample of every run.

**5 drones: one FAIL and one PASS of the same show. Both are counted.**

| | `final-n5-p1` | `final-n5-p2` (rerun) |
|---|---|---|
| Result | FAIL, fleet in RTL | PASS, DISARMED |
| Per-drone peak altitude vs plan (m) | 8.0 / 10.4, 10.5 / 13.6, 9.9 / 15.2, 10.5 / 10.5, 10.0 / 12.0 | 10.41 / 10.4, 13.62 / 13.6, 15.22 / 15.2, 10.54 / 10.5, 12.04 / 12.0 |
| Max tracking error per drone (m) | 8.7 to 12.0 | 0.18 to 0.33 |
| Failsafes | geofence, then proximity, then RTL | none |
| RTF over the show | 0.575 | 0.998 |
| Longest sim stall (tool) | 0.19 s | 0.03 s |
| Min Gazebo separation (m) | 2.142 | 2.150 |
| Host load before / at start / at end (1 min) | 2.65 / 3.78 / 47.90 | 2.91 / 5.28 / 8.84 |
| Host load before (15 min average) | 2.08 | **51.35** |
| Docker VM minimum available memory / swap-out | 2,099 MiB / 0 | 1,947 MiB / 0 |
| Per-run `docker stats` and VM CPU samples | none were taken | 39 samples, every 5 s (`final-n5-p2.stats.jsonl`) |

**RTF through the run, in 10 s blocks of show time** (computed the same way for both
runs, from the Gazebo pose samples; `final-n5-rtf-by-block.json`):

| Show time (s) | up to 30 | 30-40 | 40-50 | 50-60 | 60-70 | 70-130 | 130-250 |
|---|---|---|---|---|---|---|---|
| `final-n5-p1` | 1.00 | 0.97 | **0.35** | 0.30 | 0.29 | 0.25-0.29 | 0.17-0.29 (never recovered) |
| `final-n5-p2` | 1.00 | 1.00 | 1.00 | 1.00 | 0.98 | 1.00 | run ended at ~130 s |

**What the first run looked like.** All five drones fell behind plan together: the
first sample over 2 m of error is at show time 45.0 to 46.2 s for every drone, each
about 2 m below its planned altitude. The sim's real-time factor fell from 1.0 to 0.35 in
the block starting at show time 40 s and stayed at 0.17 to 0.30 for the rest of the
flight, so the fleet, streamed in real time, was ahead of a slowed sim. PX4 logged an
attitude failure on drone 1 (19:30:35 UTC) and `Accel #0 fail: TIMEOUT` on drone 2
(19:30:41 UTC) mid-flight (the show started at 19:29:31 and reached SHOW_COMPLETE at
19:31:11). The tool's stall detector did not fire (the sim kept moving, slowly), there was
no swap and memory stayed above 2 GiB inside the VM, so the run does not meet the
"swapped, stalled or failed to start" rule for being discarded, and it is counted.

**Why it failed is not established.** What the records show:

- The failure is a sudden, sustained, fleet-wide slowdown of the sim, not drones
  diverging one at a time. The same show, on the same commit and the same rig, flew
  cleanly at RTF 1.00 for its whole length in the rerun.
- The host was under pressure around the first run, but the pressure is not measured in
  the run record. Host 1-minute load was 47.9 when run 1 ended. When checked afterwards
  (about 01:58 IST) the host had 11,993 of 15,689 MiB in use and 3,581 of 4,095 MiB of
  swap used, and the Ollama log records "disabling mmap for llama-server load due to host
  memory pressure" at 00:57:45 IST, about a minute and a half before run 1 started.
  While the rerun waited for the gate, other jobs were running on this machine (a pytest
  run, a `sim_scale_site.py` run and an Ollama `llama-server`); which of them were
  running during run 1 was not recorded. The Docker VM's own swap check cannot see the
  host swapping the VM's memory out.
- **This is a lead, not a finding.** No process list or host memory was sampled during
  run 1, so that run's slowdown is NOT VERIFIED as host contention. The gate was on
  1-minute load only and did not look at host memory.

**The rerun was not on a quiet host either.** It started with 1-minute load 2.91 (under
the gate), but the 15-minute average was 51.35, so the host had been very busy just
before. Load then rose to 5.3 at the start of the flight and 8.8 at the end, while the
`tara-sitl-final` container used about 2.3 CPUs and the Docker VM about 20% of its 16
vCPUs. So other work on the host was running during the rerun, and it still held RTF 1.00.

**What the rerun sampler did not capture.** The sampler also tried to read Gazebo's
world statistics from inside the container every 5 s; no Gazebo stats topic was found
from that `docker exec` (the cause was not checked; the missing Gazebo transport
environment is the likely one), so there is no RTF time series from the sampler. The RTF figures above come from the Gazebo pose samples, not from it. Host
memory and the host process list were not sampled in either run. The sampler recorded
`docker stats` (container CPU and memory), the Docker VM's CPU jiffies, load, available
memory and swap-out, and the host 1/5/15-minute load.

**What this does and does not show.**

- On this commit the 5-drone show flew to a clean PASS once and failed once, 1 of 2.
  Two runs do not say how often it fails. The earlier 5-drone passes (sections E and F)
  were a different show file.
- It does not repeat the drone-3 event from section F (one drone armed, never took off).
  In the failed run all five drones took off and flew, then lagged.
- The 3-drone result is the same show as section E and passes the same way.
- **#172 is not called accepted.** A gate that also checks host memory pressure and
  other running jobs would make a quiet-host claim checkable; the present one does not.

**Records.** The tool JSONs for the five counted runs, `final-gate-summary.json` (the
per-drone check, `launch_failures`, memory, host load and log presence for all six
attempts, including the voided one), `final-n5-p2.stats.jsonl` and
`final-n5-rtf-by-block.json` are committed here. CSVs, signed shows, PX4 ulogs and
console logs, and the GCS black-box files are on the dev machine in
`~/Desktop/tara-sitl-scratch-final/runs`. Nothing in this PR changes product code.

## Findings

These are filed here and on #172. None was fixed in this PR, per the lane
rules.

**F1. Gazebo spawn axis does not match the GCS fleet frame (Gazebo/sim side;
safety-relevant).**
- **What's wrong:** `sim/px4/launch_fleet.sh` spawns follower *i* at Gazebo
  pose `0, 2.2·i`, and `sim/px4/generate_world.py` draws the pads at the same
  coordinates (`launch_grid` (x, y) copied into Gazebo (x, y)). Gazebo is
  ENU, so that line runs **north** in PX4's NED frame. The GCS pad offsets
  (`drone_manager._pad_offset` → `launch_grid`) put drone *i* 2.2·*i* m
  **east**.
- **Effect:** each drone flies the right shape in its own local frame, so the
  GCS's own telemetry looks right, but the formation is wrong in the world.
  Gazebo saw 0.34–0.56 m minimum separation against a certified 2.05 m, and
  one collision. The GCS proximity and geofence monitors run on the same
  wrong positions, so they miss the real near-misses.
- **Proposed fix:** spawn at `"${Y_OFFSET},0,…"`, and swap x/y in the world
  generator's pad poses. Section B is that fix applied on the rig.
- **Real hardware:** before the first multi-drone flight, confirm the physical
  pad layout is east-west, as the GCS assumes.

**F2. Mission upload fails on stock PX4 SITL, which blocks every show launch
(GCS, `drone_manager.py`: Tab 2's lane).**
- **What's wrong:** `MISSION_REMOTE_DIR = "/fs/microsd/tara"`. On SITL the
  MAVLink FTP root is the instance's `rootfs/<i>/` folder, which has no
  `fs/microsd`, and `ftp.create_directory` is not recursive. Its error is
  swallowed, and the upload then fails with `FILE_DOES_NOT_EXIST`.
- **Effect:** all missions are FAILED, `launch_gate()` refuses to arm, and
  `execution="streamed"` cannot be reached either. The streamed path does not
  use the mission, but the gate still requires it.
- **Workaround:** the rig workaround above.

**F3. Show start: the GCS holds formation at altitude but Studio shows start
on the pad (Studio/GCS interface).**
- **What's wrong:** the start gate needs FORMATION_HOLD, which means every
  drone at `altitude_layers()` (2, 4, 6 … m). A Studio sequencer show's first
  waypoint is the pad at z = 0, with its own takeoff leg.
- **Effect:** the first streamed setpoint pulls each drone down toward its
  pad. Drone 0 dropped from 1.9 m to 0.7 m within about 1.3 s, then followed
  the takeoff leg. The dive gets longer with hover altitude: about 3.7 s for
  drone 2 at 3 drones, and about 9.8 s for drone 9 at 10. All drones dive
  vertically over separate pads, so separation was not affected in these
  runs. But the first seconds of every show are not what Studio planned,
  and a show whose first waypoint is not directly above the pad would cut
  across the formation.
- **Fix options:** the show should start where the GCS parks the fleet, or the
  GCS should start shows from the pad.

**F4. Sim speed limits streamed shows above 8 drones on this machine.**
- **What's wrong:** the GCS streams setpoints on wall-clock time. When Gazebo
  falls below real time, the drones fall behind the plan.
- **Effect:** at 9 drones the real-time factor was 0.84 and steady error
  0.86 m. At 10 it was 0.65, steady tracking error grew to 3.5 m, and Gazebo
  separation dropped to 1.97 m (below Studio's 2.05 m, still above the 1.5 m
  abort).
- **Note:** this is a property of the rig, not a software bug. It is the
  documented SITL cap (~10) showing up as tracking lag rather than as a
  crash.

## Not done

- **RViz and the recording** (see Setup).
- **The acceptance decision.** Sections D, E and G are evidence. #172 is **not**
  called accepted: in section G the 5-drone show failed once and passed once. Harsh decides what meets #172.
- **`execution="onboard"`.** Stock PX4 SITL has no onboard show executor (out
  of scope per #172).

## Reproduce

Run inside a container from `taramandal-px4-sitl:v1.16.2` with the checkout at
`/work`. The GCS and the tool must run in the same container: Docker Desktop's
`--network host` is the VM's network, not the host's. Then:

```bash
# once: Studio signing key, trusted key in a scratch tara.toml, an operator
./tara keygen /scratch/studio.pem --signer "Studio SITL #172"   # paste its line into /scratch/tara.toml [security].trusted_keys
PYTHONPATH=packages/tara-gcs:packages/tara-core python -c 'from pathlib import Path; from tara_gcs.auth import OperatorStore, Role; OperatorStore(Path("/scratch/operators.json")).add("sitl172", Role.FLIGHT_COMMANDER, "<passphrase>")'

# per run: new container, fresh stack (no mkdir needed since F2, #217)
GCS_CONFIG_PATH=/scratch/tara.toml STUDIO_SIGNING_KEY=/scratch/studio.pem \
GCS_OPERATORS_PATH=/scratch/operators.json PX4_AUTOPILOT_DIR=/home/px4/PX4-Autopilot \
  ./tara up --backend px4 -n 3 &
# once /api/status shows connected == 3:
TARA_PASSPHRASE='<passphrase>' python3 tools/sitl_planned_vs_actual.py run --show examples/diwali/segments/diwali_3drone_segment.json --out /scratch/runs --label p1 --commit "$(git rev-parse HEAD)"
python3 tools/sitl_planned_vs_actual.py summarize /scratch/runs/*.json
```

Each run writes:
- `<label>.json`: everything in the tables above, plus the fleet lifecycle
  history;
- `<label>.gazebo_fleet_frame.csv`: Gazebo truth with the plan alongside,
  thinned to 10 Hz;
- `<label>.gcs_telemetry.csv`: the GCS's own view;
- the signed show and its `.sig.json`.

The JSON files in [`docs/sitl-planned-vs-actual/`](sitl-planned-vs-actual/)
are those records. The CSVs and show files are left out for size. The
start-transient fields of runs flown before that metric was added were
filled in with `rescore` from the 10 Hz CSVs (marked
`rescored_from_csv_10hz`).
