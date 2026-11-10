# Diwali 269-drone flat 2D show

A 13-scene, 972.1 s Diwali show for 269 drones, drawn flat in one plane (drones side-on form a line, so every
drone is spent on the picture). Scenes: Golden Spiral, Diya (flame sways), Happy Diwali, Lord Ganesha (ears, hand,
lotus bloom, swastikas turn), Shri Ram, Shri Ram advances, the baan is loosed, Ripple Rings, Rangoli, Firecrackers
and Sparkles, Ayodhya Mandir (flag flutters), Lakshmi with Lotus (hands and seat move, **the crown and halo stay
still**), Grand Finale. Lights are baked per waypoint (takeoff glow, dissolve, dark flight, per-scene effects).
Every drone that a figure does not need is put to use as a motif (spare drones are never parked).

## What is in this folder

| Path | What |
| --- | --- |
| `formations/` | The 13 final scene files (`s01`-`s13.json`, point sets with roles and colours) and `summary.json`. This is the source of the show. |
| `build_show.mjs` | Builds the show from `formations/` with the Studio engine, bakes the light choreography and the motion blocks, and validates it through the Studio API. |
| `measure_motion.py` | Per-scene speed / acceleration / jerk of the moving parts, by role. |
| `timeline.json`, `validation_result.json` | Scene timing and the validator result of the build described below. |
| `provenance/` | The two scripts that produced `formations/` from earlier scene folders. **Provenance only:** the earlier folders are not in the repo. |

The exported show (about 200 MB of waypoints) is **not** committed; it is rebuilt from the files above in about 30 s.

## Rebuild

```bash
# 1. a Studio API from a checkout that includes the 60 m site profile (PR #255, see Dependencies)
STUDIO_PORT=8768 PYTHONPATH=packages/tara-core:packages/tara-studio python -m tara_studio.server &
# 2. build + validate
node shows/diwali-269/build_show.mjs . shows/diwali-269/formations http://127.0.0.1:8768 /tmp/diwali269
```

The output folder gets `choreo.json` (the show), `timeline.json`, `target_idx.json` (so Studio can play it without
Generate Sequence) and `result.json` (the `/api/validate` answer). Export through Studio's gated `/api/export` to get the
file the GCS loads.

## Dependencies - read before merging

* **Needs the per-show site profile of PR #255** (`scale400`: 60 m radius, 27 pad columns). On current `main` the engine
  refuses the formations (`Exact-points formation (n=269) reaches 51.1m from the site origin, beyond the site radius
  (30m)`). That refusal is the safe behaviour; this PR adds no way around it. Merge #255 first.
* The GCS must run with the show's own geofence envelope (also #255).

## Verified (sim only - nothing here has been flown)

Rebuilt from these files on a clean checkout of the #255 branch (no local patches), validated through the real
`/api/validate`:

| Check | Result |
| --- | --- |
| Validator | **PASSED**, `feasibility_certified: true` |
| Violations | **0** in each of proximity, velocity, acceleration, yaw rate, altitude, ground, jerk |
| Minimum separation | 2.05 m (limit 2.0 m; scene formation spacing >= 2.85 m) |
| Maximum speed / acceleration / jerk | 3.02 m/s / 3.00 m/s^2 / 17.32 m/s^3 |
| Altitude | 0 to 112 m (ceiling 120 m) |
| Duration | 972.1 s, 9,722 waypoints per drone |

Motion of the moving parts (from `measure_motion.py`; the rest of each scene is static):

| Scene | Moving drones | Max speed | Max accel | Max jerk | Worst role |
| --- | ---: | ---: | ---: | ---: | --- |
| Diya | 54 | 3.02 | 3.00 | 5.00 | flame_tip |
| Happy Diwali | 36 | 1.38 | 1.70 | 8.49 | burst |
| Lord Ganesha | 117 | 2.98 | 2.80 | 6.80 | hand |
| Firecrackers & Sparkles | 12 | 2.44 | 1.12 | 2.06 | chakri |
| Ayodhya Mandir | 6 | 1.55 | 2.80 | 5.50 | mflag |
| Lakshmi with Lotus | 119 | 2.16 | 2.92 | 7.57 | hlotus |
| Grand Finale | 54 | 1.26 | 1.46 | 4.24 | burst |

Ram and the baan, measured on the planned trajectory: the 185 Ram drones advance a rigid 5.00 m and the 17 arrow
drones fly 20.00 m further (25.00 m from the start); the 252 other lit drones stay where they are. The line
`baan check ... max deviation 5.00 m` that `build_show.mjs` prints is a stale self-check and is **not** a trajectory
error.

Flown through the GCS **sim backend** (`tara_sim`, in-process, 269 drones, signed show, SIMULATION pre-arm profile),
with the GCS fixes from the companion PRs and a dashboard attached: `SHOW_COMPLETE` after 980.2 s against 972 s planned,
0 aborted drones, no emergency transition, 0 proximity-failsafe trips, landed and disarmed. Flown minimum separation
(GCS blackbox, 10 Hz) **1.97 m** after the first 25 s and 2.00 m while settling from the launch layers. That is
slightly below the 2.0 m planned limit and well above the GCS failsafe (1.5 m); it is tracking error of the
simulator, and an earlier run of the same show gave 1.96 m. Treat 1.95-2.0 m as the real flown figure.

## Known limits

* **Sim only.** No PX4 SITL, Gazebo or hardware flight of this show.
* **The first ~20 s of a GCS run is a settle, not the Studio takeoff.** The GCS launch sequence first climbs the drones
  in batches to separate altitude layers; the show then starts from those layers. In that settle the dashboard's
  deviation HUD reads hundreds of drones off trajectory (up to about 94 m) until they reach the plan (settled at
  about 21 s in the sim); it is all nominal afterwards.
* Run on a loaded machine, the GCS event loop stalls and the simulated fleet falls behind the plan; the companion
  GCS PRs remove the two main causes. A full 269-drone run still freezes the loop for about 0.3 s every 5 s
  (state snapshot write) and 3 s once at show start.
* The scene spacing floor is 2.85 m (the engine refuses less); some single figures have weaker silhouettes than
  others (see the scene files).
