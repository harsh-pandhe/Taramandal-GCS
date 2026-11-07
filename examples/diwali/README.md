# Diwali 2026 show

A storyboard for the 8 Nov demo, built with Studio's existing engine only (no engine code changed) and certified through the real `/api/export`.

| Storyboard | Drones | Scenes | Use |
|---|---:|---|---|
| `storyboard_35.json` | 35 | diya, rangoli, ring, sphere burst | the near-term show |
| `storyboard_35_lettering.json` | 35 | DIWALI, then the above | certified, but lettering is not legible (see below) |
| `storyboard_100.json` | 100 | HAPPY, DIWALI, diya, rangoli, sphere burst | the full greeting |
| `storyboard_35_figures.json` | 35 | diya, sphere burst, diya | **figures-only fallback**, no spelled greeting |
| `storyboard_100_figures.json` | 100 | diya, rangoli, sphere burst, diya | **figures-only fallback**, no spelled greeting |
| `storyboard_500.json` | 500 | ring, sphere burst | sim scale run only |
| `storyboard_3.json` | 3 | ring, sphere burst, ring | segment for Gazebo/PX4, see `segments/README.md` |

Front-view previews of every figure are in `previews/`.

## PX4-safe pacing (7 Nov): the three shows that may be flown, re-built and re-certified

PR #244 capped what Studio plans to PX4's flown envelope (descent 1.2 m/s against PX4's `MPC_Z_VEL_MAX_DN` 1.5, climb 2.4 against `MPC_Z_VEL_MAX_UP` 3.0, acceleration 3.0 against `MPC_ACC_HOR` 3.0), because a plan above those limits is not flown as planned. The caps lengthen the descents, so the shows below were re-built on `origin/main` 1d2d3a4 (default Standard pacing) and re-certified through the real `/api/export` as flight exports (json and tara, HTTP 200, `preview_only: false`, 0 violations in every category). `tools/diwali_build.mjs` now also reports the peak climb and descent rate of the exported plan and fails the build if either is above PX4's default.

| Show | Drones | Real length now (before the caps) | Nominal | Min separation | Peak altitude | Peak climb / descent | Max velocity / accel / jerk | .tara |
|---|---:|---:|---:|---:|---:|---|---|---:|
| figures-only: diya, sphere burst, diya | 35 | **207.6 s** (197.8 s) | 76 s | 2.05 m | 34.1 m | 1.6 / 1.2 m/s | 1.75 m/s / 2.45 m/s² / 17.3 m/s³ | 1.2 MB |
| figures-only: diya, rangoli, sphere burst, diya | 100 | **324.8 s** (269.6 s) | 82 s | 2.05 m | 62.8 m | 2.4 / 1.2 m/s | 2.6 m/s / 3.0 m/s² / 17.3 m/s³ | 5.2 MB |
| lettered: HAPPY, DIWALI, diya, rangoli, sphere burst | 100 | **416.9 s** (352.5 s) | 94 s | 2.05 m | 62.8 m | 2.3 / 1.2 m/s | 2.3 m/s / 2.45 m/s² / 17.3 m/s³ | 6.7 MB |

The 35-drone show is 10 s longer, the 100-drone shows 55 s and 64 s longer. Everything else is unchanged: separation, altitude and the figures themselves.

T1 `tara_sim` re-flight of all three (`results/sim_35_figures.json`, `sim_100_figures.json`, `sim_100.json`; run at `nice -n 19`, host load average about 1): no proximity or geofence breach; flown minimum separation after settling 2.048 / 2.045 / 2.041 m; tracking error after settling 0.02 / 0.03 / 0.04 m mean and 0.08 / 0.24 / 0.36 m p99; real-time factor 1.00 / 0.95 / 0.94. This is the kinematic simulator only, not SITL or Gazebo: it does not model PX4's climb, descent or acceleration limits, so it cannot show whether the plan is flown as planned. That needs a PX4 run, which has not been done for these shows.

The sections below were written before the caps. Their numbers for the rebuilt shows are superseded by the table above; the 3, 35 (with ring), 35-with-lettering and 500-drone builds and their sim runs were **not** re-run and predate the caps.

## Measured results

Everything below was measured, not estimated. Raw results: `results/certify_*.json` (export) and `results/sim_*.json` (sim flight).

**Certification** (`tools/diwali_build.mjs`): built with `buildSequencedShow` at 10 Hz and the local optimal assignment chain, then posted to the real `/api/export` as a flight (non-preview) export in both `json` and `tara`. All five builds: HTTP 200, `preview_only: false`, 0 violations in every category (proximity, velocity, acceleration, yaw rate, altitude, ground, jerk), minimum separation at least 2.0 m, peak altitude well under the 120 m ceiling.

| Drones | Duration | Min separation | Peak altitude | Max jerk | .tara size |
|---:|---:|---:|---:|---:|---:|
| 3 † | 84.2 s | 2.20 m | 14.7 m | 15.0 m/s³ | 40 KB |
| 35 (with ring) † | 209.5 s | 2.05 m | 34.4 m | 17.3 m/s³ | 1.2 MB |
| 35 + lettering † | 230.8 s | 2.05 m | 34.4 m | 17.3 m/s³ | 1.3 MB |
| 100 (lettered) | 416.9 s | 2.05 m | 62.8 m | 17.3 m/s³ | 6.7 MB |
| 500 † | 216.4 s | 2.05 m | 75.2 m | 17.3 m/s³ | 17 MB |

† Built before the PX4-safe caps and not re-run; the duration of these would be longer now. The 100-drone lettered row is the current build.

Minimum separation is measured on the exported JSON, independently of the validator, over every sample (and four linear sub-steps between samples up to 150 drones; samples only at 500). Durations are 1.3x to 5x the typed scene times (nominal 64 s at 3 drones, 74 s at 35, 94 s at 100, 42 s at 500); the larger the fleet, the more the velocity and jerk limits stretch each leg.

**Sim flight** (`tools/diwali_sim_scale.py`): the exported JSON flown through `DroneManager` on `backend="sim"` (upload, `launch_sequence`, onboard start), positions sampled at 5 Hz.

| Drones | Real-time factor | Late 50 Hz ticks | Worst tick gap | Missed 10 Hz control ticks | Proximity breach | Flown min separation after settling | Tracking error after settling (mean / p99 / max) |
|---:|---:|---:|---:|---:|:--:|---:|---|
| 3 | 1.00 | 0.0% | 25 ms | 0.0% | no | 2.20 m | 0.05 / 0.7 / 4.9 m |
| 35 | 0.98 | 0.4% | 303 ms | 2.1% | no | 2.05 m | 0.03 / 0.2 / 3.8 m |
| 100 | 0.93 | 0.4% | 836 ms | 2.1% | no | 2.04 m | 0.06 / 0.5 / 4.6 m |
| 500 | 0.78 | 4.4% | 1.73 s | 9.6% | no | 2.04 m | 0.20 / 2.2 / 4.4 m |

Once the fleet is on the plan it stays on it: separation never fell below 2.04 m and no drone left the geofence. The sim itself is the weak point at show scale. `tools/benchmark_fleet_scale.py`'s 15 s synthetic show runs at 1.00x at every size, but a real show with thousands of waypoints per drone does not. The mission executor alone costs 0.5 ms per tick at 100 drones and 4 ms at 500 (profiled in isolation, 20 ms budget), so it does not explain the shortfall by itself; the cause is not yet known. At 500, 253 of 500 drones were still airborne when the sampling window closed at the planned duration, consistent with physics running 22% slower than the UTC mission clock.

## Figures-only fallback (no spelled greeting)

**Real show lengths: 208 s at 35 drones and 325 s at 100 drones, against a nominal 76 s and 82 s** (the typed hold and transition times; 198 s and 270 s before the PX4-safe caps). The flown show is 2.7x to 4.0x longer because the velocity, acceleration and jerk limits stretch every leg; plan the demo slot from the real length, not the nominal one.

For the case where the lettering is dropped. Same engine, same fixtures, no text. At 100 drones: diya, rangoli, sphere burst, then the diya again as the finale. At 35 drones the rangoli is left out, because it reads as a scatter of points at that size, and its time goes to the diya and the sphere: diya, sphere burst, diya. The 35-drone show uses the stock `diya_flame.svg`; the 100-drone show uses `diya_festive.svg` (the stock diya is refused at 100). Previews: `previews/figures_only_35_drones.png`, `previews/figures_only_100_drones.png`.

Both certify through the real `/api/export` as flight exports (json and tara, HTTP 200, `preview_only: false`, 0 violations, `results/certify_*_figures.json`).

| Drones | Duration | Min separation | Peak altitude | Max velocity / accel / jerk | .tara size |
|---:|---:|---:|---:|---|---:|
| 35 | 207.6 s | 2.05 m | 34.1 m | 1.75 m/s / 2.45 m/s² / 17.3 m/s³ | 1.2 MB |
| 100 | 324.8 s | 2.05 m | 62.8 m | 2.6 m/s / 3.0 m/s² / 17.3 m/s³ | 5.2 MB |

T1 `tara_sim` flight of both: see the PX4-safe pacing section above. The launch-layer dive described below is present here too (settles in 6.5 s at 35, 14.1 s at 100).

What reads and what does not, from the previews: the diya, the rangoli and the sphere read clearly at 100 drones. At 35 the diya and sphere read; the rangoli (35 points on a flower) did not, which is why it is out of the 35-drone show. The heart and the ring were left out: Studio lays them out flat in plan view, so from a front audience view they are a grid of dots. They only read from above (for example the full-screen preview camera).

## What did not work, and why

- **Lettering is not legible at 35 drones.** A drone needs roughly 15 or more points to read as a letter at the 2.0 m minimum spacing. DIWALI at 35 is about 6 per letter. `storyboard_35_lettering.json` certifies but should not be flown as a greeting. At 100 drones HAPPY and DIWALI are about 20 and 16 per letter: the strokes are visible, but it is marginal and needs a human look at `previews/figures_100_drones.png`.
- **Studio's text font cannot spell the greeting.** It is seven-segment (digits plus A B C D E F H L P U) and caps a caption at 8 characters; Y, I and W fall back to an "8". The lettering is therefore SVG paths loaded through the SVG importer (`tools/diwali_make_lettering.py` generates them).
- **The stock `diya_flame.svg` is refused at 90 or more drones** (its outline is too short to hold 2.0 m). The 100-drone show uses `diya_festive.svg`: the same lamp and flame plus a halo ring. The 35-drone show uses the stock diya.
- **At 500 only the ring and sphere build.** SVG figures stop at about 110 to 120 drones (the 30 m site radius): HAPPY and DIWALI build at 100 but not at 110, the festive diya at 110 but not at 120, the rangoli at 120 but not at 150.
- **`launch_sequence` and the show start do not meet.** It stacks drones on hover layers that cycle up to the 120 m ceiling (54 m at 35 drones, 120 m at 100 and 500), but a Studio show starts on the pad at t=0, so the onboard start sends the whole fleet down to the pads. Before settling the tracking error reaches 54 m (35), 120 m (100) and 120 m (500). Settling takes 7 s, 15 s and 19 s. Separation held throughout (at least 2.12 m before settling) and no proximity breach was seen, but it is a descent through the show volume, not a takeoff.

## Issue candidates for Harsh (none opened)

1. Text shape: no letters beyond the seven-segment set, 8-character cap. A real glyph set, or a per-letter drone budget, is needed for any greeting.
2. SVG importer: every contour gets its own depth plane 2.5 m apart, so multi-stroke lettering spends the 30 m site radius on depth. There is no way to keep strokes coplanar.
3. Figures are refused only after the fact (diya at 90). The Studio UI could show each figure's supported drone range up front.
4. `launch_sequence` hover layers versus a show that starts on the pad (above). Either launch to the show's t=0 positions or start the mission from the hover layer.
5. Sim fidelity at show scale: 0.93x at 100 and 0.78x at 500 on a real show, 0.3 to 1.7 s event-loop stall at the onboard start (35 to 500 drones), and 110 / 510 asyncio tasks at that moment (one offboard release per drone) against #51's budget of 50.
6. 500-drone figures: only parametric shapes build. Real 500-drone figures need the split-formation work (#166).

## Rebuild

```bash
python tools/diwali_make_lettering.py            # regenerate the SVG fixtures
tools/diwali_run_all.sh                          # build + certify every storyboard into dist/diwali/
python tools/diwali_sim_scale.py dist/diwali/diwali_100.json dist/diwali/diwali_500.json
```

**Set `PYTHON` to the repo venv** (for example `PYTHON=.venv/bin/python tools/diwali_run_all.sh`). In a fresh worktree there is no `.venv`, the script falls back to the system `python3`, which has no uvicorn, and the backend never starts (the build then fails with a connection-refused error).

`tools/diwali_run_all.sh` starts its own Studio backend and stops it afterwards; `DIWALI_SIZES="35 100"` limits it to some storyboards. Builds are deterministic: the committed 3-drone segment is byte-identical to a fresh build. The sim runs take real time (the 500-drone run is about 11 minutes, 400 s of it the launch stagger).
