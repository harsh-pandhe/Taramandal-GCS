# Diwali 2026 show

A storyboard for the 8 Nov demo, built with Studio's existing engine only (no engine code changed) and certified through the real `/api/export`.

| Storyboard | Drones | Scenes | Use |
|---|---:|---|---|
| `storyboard_35.json` | 35 | diya, rangoli, ring, sphere burst | the near-term show |
| `storyboard_35_lettering.json` | 35 | DIWALI, then the above | certified, but lettering is not legible (see below) |
| `storyboard_100.json` | 100 | HAPPY, DIWALI, diya, rangoli, sphere burst | the full greeting |
| `storyboard_500.json` | 500 | ring, sphere burst | sim scale run only |
| `storyboard_3.json` | 3 | ring, sphere burst, ring | segment for Gazebo/PX4, see `segments/README.md` |

Front-view previews of every figure are in `previews/`.

## Measured results

Everything below was measured, not estimated. Raw results: `results/certify_*.json` (export) and `results/sim_*.json` (sim flight).

**Certification** (`tools/diwali_build.mjs`): built with `buildSequencedShow` at 10 Hz and the local optimal assignment chain, then posted to the real `/api/export` as a flight (non-preview) export in both `json` and `tara`. All five builds: HTTP 200, `preview_only: false`, 0 violations in every category (proximity, velocity, acceleration, yaw rate, altitude, ground, jerk), minimum separation at least 2.0 m, peak altitude well under the 120 m ceiling.

| Drones | Duration | Min separation | Peak altitude | Max jerk | .tara size |
|---:|---:|---:|---:|---:|---:|
| 3 | 84.2 s | 2.20 m | 14.7 m | 15.0 m/s³ | 40 KB |
| 35 | 209.5 s | 2.05 m | 34.4 m | 17.3 m/s³ | 1.2 MB |
| 35 + lettering | 230.8 s | 2.05 m | 34.4 m | 17.3 m/s³ | 1.3 MB |
| 100 | 352.5 s | 2.05 m | 62.8 m | 17.3 m/s³ | 5.6 MB |
| 500 | 216.4 s | 2.05 m | 75.2 m | 17.3 m/s³ | 17 MB |

Minimum separation is measured on the exported JSON, independently of the validator, over every sample (and four linear sub-steps between samples up to 150 drones; samples only at 500). Durations are 1.3x to 5x the typed scene times (nominal 64 s at 3 drones, 74 s at 35, 94 s at 100, 42 s at 500); the larger the fleet, the more the velocity and jerk limits stretch each leg.

**Sim flight** (`tools/diwali_sim_scale.py`): the exported JSON flown through `DroneManager` on `backend="sim"` (upload, `launch_sequence`, onboard start), positions sampled at 5 Hz.

| Drones | Real-time factor | Late 50 Hz ticks | Worst tick gap | Missed 10 Hz control ticks | Proximity breach | Flown min separation after settling | Tracking error after settling (mean / p99 / max) |
|---:|---:|---:|---:|---:|:--:|---:|---|
| 3 | 1.00 | 0.0% | 25 ms | 0.0% | no | 2.20 m | 0.05 / 0.7 / 4.9 m |
| 35 | 0.98 | 0.4% | 303 ms | 2.1% | no | 2.05 m | 0.03 / 0.2 / 3.8 m |
| 100 | 0.93 | 0.4% | 836 ms | 2.1% | no | 2.04 m | 0.06 / 0.5 / 4.6 m |
| 500 | 0.78 | 4.4% | 1.73 s | 9.6% | no | 2.04 m | 0.20 / 2.2 / 4.4 m |

Once the fleet is on the plan it stays on it: separation never fell below 2.04 m and no drone left the geofence. The sim itself is the weak point at show scale. `tools/benchmark_fleet_scale.py`'s 15 s synthetic show runs at 1.00x at every size, but a real show with thousands of waypoints per drone does not. The mission executor alone costs 0.5 ms per tick at 100 drones and 4 ms at 500 (profiled in isolation, 20 ms budget), so it does not explain the shortfall by itself; the cause is not yet known. At 500, 253 of 500 drones were still airborne when the sampling window closed at the planned duration, consistent with physics running 22% slower than the UTC mission clock.

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

`tools/diwali_run_all.sh` starts its own Studio backend and stops it afterwards; `DIWALI_SIZES="35 100"` limits it to some storyboards. Builds are deterministic: the committed 3-drone segment is byte-identical to a fresh build. The sim runs take real time (the 500-drone run is about 11 minutes, 400 s of it the launch stagger).
