# #171 B-1: pilot takeover during the landing wait, on real PX4 SITL

Review of #211 (B-1). PX4 v1.16.2 + Gazebo, one drone, GCS inside the container.
`landing_takeover.py` hovers to 4 m, waits until the GCS has put PX4 in LAND, then takes
over with a MANUAL_CONTROL stream + `start_position_control()` (POSCTL) and watches PX4's own
mode/armed stream (an independent MAVSDK client on 14550) for 25 s.

| Run | GCS code | Result |
|---|---|---|
| `run_main.log` | origin/main `6f18df0` | POSCTL held for 9 s, then the GCS re-sends offboard.stop + land (mode LAND at +10 s) and the drone descends and lands over the pilot; HTTP 500 "Touchdown disarm could not be confirmed" |
| `run_fixed.log` | this branch | HTTP 500 "operator took over during the landing (flight mode POSCTL)" straight away; PX4 stays POSCTL, armed, 3.45 m for the full 25 s (the LAND at the end is the script's cleanup) |
| `run_nominal.log` | this branch, no pilot, 2 runs | both HTTP 200 COMPLETED; PX4 reports OFFBOARD, LAND, HOLD only, none is flagged as a takeover |

Scripts expect the rig layout of the earlier #171 runs (`/scratch` = lib.py, observer.py, operators.json).
