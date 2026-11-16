# T2: stopping PX4's estimator mid-hover, before and after the UNKNOWN-mode fix (#171, #294)

A fault where PX4 keeps running but its state estimate stops: `px4-ekf2 stop` (PX4's own shell) while the single-drone hover is in its dwell.
Run in the #171 Docker rig (`docs/sitl-171-acceptance/rig/`, one fresh container per run, PX4 v1.16.2 + Gazebo Sim 8.15, headless,
`tara up --backend px4 -n 1`), with the scenario in `case_ekf_stop_mid_hover.py`. PX4's own estimate freezes with the fault, so the
vehicle's real altitude is read from **Gazebo's model pose**, the same stream the planned-vs-actual tool uses. Simulation only.

## What it found

On `origin/main` 0601bd5 the GCS answered the hover with
`Hover dwell failed: Drone 0: operator took over (flight mode left OFFBOARD for UNKNOWN); the sequence is yielding without commanding the vehicle`
and issued **no command at all** (no recovery). Nobody took over: PX4's reported mode became `UNKNOWN`. The hover check treated any mode other than
OFFBOARD as a pilot, `UNKNOWN` included, although the landing check had already (after the #211 review) said "UNKNOWN / None are not evidence of
anything". A vehicle whose estimator has died, with nobody flying it, was therefore left alone by the GCS.

The fix is one condition in `DroneManager._check_operator_takeover` (`UNKNOWN` is not a pilot; every pilot mode is still a takeover). After it
the hover reports `Hover dwell failed: Drone 0 telemetry became stale during hover dwell` 2.6 s after the fault, and the GCS runs its safe recovery.

| Run | Commit | Verdict | Hover answer | GCS shows the freeze | Failed checks |
|---|---|---|---|---|---|
| `ekf-1` | 0601bd5 (main) | FAIL (6/7) | climb failed: operator took over ... UNKNOWN | 2.6 s | my first version of the end-state check (see below); fault landed in the climb |
| `ekf-2-before` | 0601bd5 (main) | FAIL (7/9) | dwell failed: **operator took over ... UNKNOWN**, yielding | 2.6 s | "does not blame a pilot", "reported as a lost or stale vehicle" |
| `ekf-3-after` | cc8a49b (fix) | **PASS (9/9)** | dwell failed: **telemetry became stale during hover dwell** | 2.6 s | none |
| `ekf-4-after` | cc8a49b (fix) | **PASS (9/9)** | same | 2.6 s | none |
| `ekf-5-after` | cc8a49b (fix) | **PASS (9/9)** | same | 2.6 s | none |

In the "after" runs the GCS log shows the recovery the "before" run never attempted: `Safe-recovery ground confirmation after a failed hover dwell for drone 0: ... telemetry became stale`
and `Failed to command Landing on Drone 0: COMMAND_DENIED` (with the estimator stopped, PX4 itself refuses `land`).

## What the fault does in the simulator (all five runs alike)

- Gazebo shows the vehicle on the ground within about 1 s of the fault (it falls from about 0.9 m). PX4's own state stays frozen: mode `UNKNOWN`, armed, `IN_AIR`, last altitude.
- The GCS shows the freeze after 2.6 s (`connected` 0, `armed_stale` 1) and never reports the drone disarmed; for the first 2.5 s after the fault it still shows `armed` 1 with fresh-looking telemetry, which is the staleness threshold, not a new finding.
- The fleet state stays STAGED.

## Limits

- Simulation, headless, one fault type. `px4-ekf2 stop` is a brutal stand-in for an estimator failure (a real PX4 has estimator failover and other failsafes); it shows how the GCS treats a vehicle whose mode and position stop being meaningful, not that real hardware would do this.
- `ekf-1` is kept, not deleted. Its end-state check ("ends disarmed on the ground") can never pass in this fault, because PX4's own armed state is frozen; I replaced it with the Gazebo ground-truth check and added the two "failure text" checks before the `ekf-2` onward runs, and the fault was moved from the climb to the dwell.
- This covers an estimator stop, not a single isolated telemetry field freezing while every other stream stays live.
- It is not a PX4-denied `land` test while airborne: the denied `land` here came from the GCS's own recovery, with the vehicle already on the ground.
- Run on a quiet host (load below 3 at each launch).
