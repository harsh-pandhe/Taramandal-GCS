# PR #240 on PX4 SITL: OFFBOARD confirmation and the planned-fleet formation gate (#171)

Five-drone launches of the Diwali segment on PX4 v1.16.2 SITL + Gazebo (`taramandal-px4-sitl:v1.16.2`),
one fresh container per run, flown with `tools/diwali_t2_sitl.sh 5 <label> dist/diwali58/diwali_n5.json`
on the PR head (`972968a`). Every run flew the same signed show
(sha256 `51edf38fc34cc4fa3b3efc0aa5163730b281477b61b1b360707988b6b130b6af`, 84.8 s, Studio PASSED,
feasibility certified). `dist/` is gitignored, so the show itself is not committed here.

## Runs

| Run | Host load before / after (1 min) | Verdict (tool's rule) | OFFBOARD confirmed | `launch_failures` | Arm to OFFBOARD (black box, 0.1 s ticks) | TAKEOFF to FORMATION_HOLD |
|---|---|---|---|---|---|---|
| `pr240-n5-r1` | 3.56 / not recorded | PASS | 5 of 5 | none | 0.0 s on all 5 | 54.0 s |
| `pr240-n5-r2` | 1.95 / 12.52 | PASS | 5 of 5 | none | 0.0 s on all 5 | 55.2 s |
| `pr240-n5-r3` | 2.57 / 10.74 | PASS (degraded: RTF 0.852) | 5 of 5 | none | 0.0 s on all 5 | 55.2 s |
| `pr240-n5-r4` | 3.35 / 10.30 | PASS (degraded: RTF 0.863) | 5 of 5 | none | 0.0 s on all 5 | 55.7 s |
| `pr240-n5-r5` | 2.57 / 9.20 | PASS | 5 of 5 | none | 0.0 s on all 5 | 50.0 s |
| `pr240-n5-load8` (under load, `FORCE_BUSY=1`) | 9.26 / 15.74 | PASS | 5 of 5 | none | 0.0 s on 4, 0.1 s on drone 3 | 50.4 s |
| `pr240-n5-hold3` (drone 3 forced into HOLD) | 3.95 / 9.30 | ERROR, as intended: fleet held in TAKEOFF | 4 of 5 | drone 3 only | drone 3 never in OFFBOARD | never (held in TAKEOFF) |

"Degraded" is the measuring tool's label for a Gazebo real-time factor below 0.9; it does not touch the launch.
The FSM times are seconds into the black box.

**False positives:** none. Across the six healthy launches, 30 drones were judged, every one was "confirmed from
telemetry", and no `launch_failures` entry appeared on any of them. The four healthy drones in the fault run were
also confirmed and not named.

**Fault run (`pr240-n5-hold3`):** from the moment the fleet left STAGED, PX4's own commander forced drone 3 into
HOLD (`px4-commander --instance 3 mode auto:loiter` every 0.3 s for 25 s). The GCS logged
`Drone 3 launch failed: did not enter OFFBOARD: flight mode HOLD 3s after offboard start` and
`Drone 3 disarmed on its pad after a failed launch`. Drone 3's highest point in the black box is 0.02 m. The other
four climbed to their hover altitudes, and `/api/status` showed `fleet_state: TAKEOFF`, `armed: 4`,
`launch_failures: {"3": ...}`. The script that was meant to try a show start and then land had a bug (`docker exec`
without `-i`, so its Python never ran); see `pr240-n5-hold3b` for the start attempt.

## Files per run

- `result.json`: the measuring tool's record (`tools/sitl_planned_vs_actual.py`).
- `stdout`, `tool.txt`: the tool's and the rig script's output (`r1` was started by hand, so it has no `tool.txt`).
- `launch_lines.txt`: every GCS log line about OFFBOARD confirmation, launch failures, disarm or land.
- `bb.txt`: per-drone arm time, OFFBOARD time and highest point, plus the FSM transitions, from the black box.
- `blackbox.jsonl.gz`: the GCS black box (`tara_gcs.blackbox.read_blackbox`).
- `gcs_stack.log.gz`: the whole `./tara up` output inside the container (GCS, fleet, web).
- `gcs_telemetry.csv.gz`, `gazebo_fleet_frame.csv.gz`: what the GCS saw and what Gazebo flew.
- `px4/px4_<i>.log`, `docker_logs.txt`, `docker_inspect.json`: PX4's console per drone and the container's own logs
  and state. **Only `r5` has these.** Each run's container is replaced by the next, and these runs were flown before
  the logs were being saved; `r5`'s container survived a host reboot stopped, and they were copied out of it.
  `docker_logs.txt` is empty by design: the container's main process is `sleep infinity`, and the stack's output is
  in `gcs_stack.log.gz`. PX4's `.ulg` flight logs (about 44 MB per drone) are kept off the repo.
