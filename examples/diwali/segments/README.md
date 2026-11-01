# 3-drone Diwali segment (hand-off to the Gazebo/PX4 tab)

`diwali_3drone_segment.json` is a certified, unsigned flight export of `../storyboard_3.json`: a rangoli ring, a sphere burst and a ring again, for 3 drones. `diwali_3drone_segment.tara` is the same show compiled.

| | |
|---|---|
| Duration | 84.2 s (843 waypoints per drone at 10 Hz) |
| Peak altitude | 14.7 m |
| Minimum separation | 2.20 m |
| Max velocity / acceleration / jerk | 1.3 m/s / 2.0 m/s² / 15.0 m/s³ |
| Certification | real `/api/export`, HTTP 200, `preview_only: false`, 0 violations (`../results/certify_3.json`) |
| Sim flight (`tara_sim`) | 1.00x real time, no proximity breach, flown min separation 2.20 m, all 3 landed (`../results/sim_3.json`) |

It was flown only in `tara_sim` (`backend="sim"`). It has not been flown against PX4 SITL or Gazebo.

- The file is unsigned. A GCS that enforces signatures will refuse the upload unless it runs with `--allow-unsigned`; to sign, export from Studio with `sign: true` and a configured signing key.
- Rebuild it with `tools/diwali_run_all.sh` (`DIWALI_SIZES=3`). The build is deterministic, so the output is byte-identical.
- Waypoints are in the shared pad frame: every drone starts on its pad at t=0, z=0 (NED, so altitude is negative z).
- Do not edit these files from the other tab; regenerate them from the storyboard instead.
