# T2: PX4-denied `land` while a show is RUNNING (airborne), on `origin/main` 66abb2b (#294, #171, #258)

The airborne half of the #171 row "PX4-denied `land`" (the ground half is `docs/sitl-171-fault-rows/`, fixed by #258). A real signed 1-drone show is flown in the
#171 Docker rig (one fresh container per run, PX4 v1.16.2 + Gazebo Sim 8.15, headless, `tara up --backend px4 -n 1`): Studio certifies it, the GCS verifies
the signature, the fleet goes STAGED, PREARM, ARMED, TAKEOFF, FORMATION_HOLD, RUNNING (streamed). About 15 s into the show (the drone at about 8 m) PX4's estimator
is stopped (`px4-ekf2 stop`), after which PX4 refuses LAND. The GCS is then asked to land, fleet-wide and single-drone, 0.4 s after the fault, while it still believes
the show is running. PX4's own verdict is taken afterwards from an independent client, so the denial is attributed to PX4 and not assumed. The scenario is
`case_denied_land_airborne_show.py`. Simulation only.

## Result: 3 of 3 PASS (12 of 12 checks each)

| Run | Observation after the fault | PX4 itself (independent client) | Fleet-wide `POST /api/land` | Single-drone `POST /api/drone/0/land` | Fleet state |
|---|---|---|---|---|---|
| `dla-1` | 7 s | `COMMAND_DENIED` | **502**, per-drone breakdown, `fleet_state_advanced: false` | **502**, `commanded: false` | RUNNING before, right after and 1 s later |
| `dla-2` | 95 s | `COMMAND_DENIED` | same, at +0.37 s | same | RUNNING throughout |
| `dla-3` | 95 s | `COMMAND_DENIED` | same, at +0.36 s | same | RUNNING throughout |

So the #258 behaviour holds in the air as it did on the ground: the GCS does not answer 200, names PX4's denial, commands nothing it cannot confirm, and never claims LANDING.
It also shows the vehicle's loss: `connected` 0 and `armed_stale` 1 about 3 s after the fault, never reading as disarmed.

## A finding this run exposed (not fixed here)

After the refused land, the **fleet state stays RUNNING for as long as it was watched (95 s), although the show is no longer being played**: the GCS log of every run shows
`Land triggered...`, `Commanding all drones to Land...`, `Trajectory playback stopped.`, `Trajectory playback loop cancelled.`, and only then
`Failed to command Landing on Drone 0: COMMAND_DENIED` (`gcs_log_excerpt.txt` in each run). `trigger_land` (and `trigger_rtl`, `trigger_estop`) call `stop_trajectory()` and `offboard.stop()`
before the vehicle's answer is known. When every drone refuses, the fleet state is deliberately not advanced (#258: LANDING would be a lie), but RUNNING is then a lie too:
the show was stopped by the operator's own command, and the lifecycle monitor only leaves RUNNING when every drone disarms. The 36 s show never reached SHOW_COMPLETE.
The operator is told by the 502 (and `/api/status` carries `trajectory_running`), so nothing is hidden, but the state label is stale. Options and a recommendation: issue #299.

## Limits

- Simulation, headless, one fault type and one drone. With the estimator stopped the simulated vehicle also falls (Gazebo showed this in the hover runs of `docs/sitl-estimator-stop/`); here the point is the GCS's answers, not the vehicle's fate.
- `px4-ekf2 stop` is a stand-in way to make PX4 refuse LAND while the GCS still thinks the show is running; it is not claimed to be a realistic failure.
- `dla-1` observed for 7 s only (its timeline was extended to 95 s for the next two runs); everything else about the three runs is the same.
- Run on a quiet host (load below 3 at each launch).
