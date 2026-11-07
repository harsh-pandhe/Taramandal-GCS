# #171: the two fault rows that were not run, on `origin/main` 1d2d3a4

Both rows from the 2026-11-04 acceptance table ("PX4-denied `land`: NOT RUN (could not be provoked)" and the cold-start
case of "rejected command (arm before preflight is green)") were run on `origin/main` 1d2d3a4, which includes #240.

**Environment (same for every run):** PX4 v1.16.2 + Gazebo Sim 8.15 in the container image `taramandal-px4-sitl:v1.16.2`
(`sha256:424c4648...2d3cb`), Docker 29.8.0 on linux/amd64, 16 CPUs, headless, 1 drone, `./tara up --backend px4 -n 1`.
One fresh container per run, removed as soon as the run's logs were copied out. GCS code is the repo at 1d2d3a4; only an
untracked cadence logger (`rig/cadence_sitecustomize.py`) is loaded next to it. No Gazebo window, no RViz, no recording,
so this is not a T2 recording claim. Host load before each run is in its `run.json` (1-minute average 1.52 or less before every run).
Ground truth for scenarios that used it is an independent MAVSDK client on PX4's GCS port 14550, not the GCS.

## Runs

| Run | Scenario | Result | What it is |
|---|---|---|---|
| `denied-land-1` | `denied_land` | NOT PROVOKED (1/2 checks) | Ladder of five conditions. PX4 accepted `land` in all five, but two rungs did not take effect (below). |
| `denied-land-2` | `denied_land_termination` | FAIL (7/8) | PX4 refused `land`; the GCS fleet-wide route did not report it. A baseline `land` ran first, so its fleet-state check was vacuous (state was already DISARMED). |
| `denied-land-3` | `denied_land_termination_clean` | FAIL (6/7) | Same without the baseline, so the fleet state started at STAGED. This is the run to cite. |
| `cold-start-1` | `cold_start` | FAIL (6/7), superseded | GCS refusals before green were captured. My own single-drone `land` at cold start put PX4 in LAND, after which the preflight stayed red for the remaining 225 s; the independent client never connected, so PX4's own answer was not captured. `px4_ulogs.tgz` omitted (17 MB, superseded); it is kept on the machine that ran it. |
| `cold-start-2` | `cold_start_late_observer` | FAIL (7/8), superseded | The independent client connected 1.1 s after the drone appeared, by which time the GCS preflight had already gone green (a 1.25 s red window), so PX4's first `arm` was accepted. |
| `cold-start-3` | `cold_start_px4_probe` | PASS (8/8) | Uses PX4's own shell client from boot, no MAVSDK client. This is the run to cite. |

A failing or superseded run is kept, labelled, and only the scenario script changed between runs.

## Row 1: PX4-denied `land` (provoked)

PX4 v1.16.2 deliberately never refuses `land` because of unmet mode requirements: `Commander.cpp` handles `VEHICLE_CMD_NAV_LAND`
with `force = true` ("always allow to switch into it such that if used as emergency mode it is always available"). It refuses
only when the vehicle is already in the termination state (`UserModeIntention.cpp`: "never allow to change out of termination
state"). `DO_FLIGHTTERMINATION` is denied when disarmed, so termination has to be commanded while armed.

What was tried (`denied-land-1`, all on the ground): healthy and disarmed; armed; sensor failure injection (baro + GPS)
- **the injection did not take effect**: both `inject` calls timed out with no acknowledgement and PX4's health stayed OK;
estimator stopped (`px4-ekf2 stop`, local position invalid) - taken effect, `land` accepted; `kill` - **did not take effect**, the
vehicle was not armed (arming was already refused after the estimator was stopped). PX4 accepted `land` in the three valid conditions.
Separately, in `cold-start-1` a single-drone `land` at cold start was accepted too (HTTP 200).

Provoked in `denied-land-2` and `denied-land-3`: arm on the pad, `terminate`, then `land` three ways.

| Route | Answer |
|---|---|
| PX4 directly (independent client) | `COMMAND_DENIED` (observer mode `UNKNOWN`) |
| GCS `POST /api/drone/0/land` | **502** with PX4's reason: `Drone 0 rejected landing: COMMAND_DENIED ...`; PX4 never entered LAND. Correct. |
| GCS `POST /api/land` (fleet-wide) | **HTTP 200** `"Emergency landing commanded to active backend (px4)."` although PX4 had refused. |

In `denied-land-3` the GCS audit log (`audit.jsonl`) shows the refused fleet-wide command moving the fleet `STAGED -> LANDING`
(trigger `POST /api/land`, operator `cmdr`) and, 0.07 s later, `LANDING -> DISARMED` (trigger `telemetry:all_disarmed`). PX4 had
already auto-disarmed after termination ("Disarmed by auto preflight disarming" in the PX4 console), which is why it corrected itself.
In a case where PX4 stays armed the fleet would stay in LANDING. Cause in code: `trigger_land` ignores each drone's result
(`_land_one` returns the error string, `_for_each_drone` gathers and discards it); the per-drone route checks it.
No code was changed. Whether to change it is for Harsh.

## Row 2: rejected command before the first green preflight (exercised)

`cold-start-3` starts the scenario the moment the stack is launched.

- The GCS API answered at 14.0 s; the drone appeared in the GCS at 14.81 s with the preflight red.
- At that moment `hover-sequence` and `launch` were both refused with **HTTP 412** and reasons ("pre-arm gates are red":
  battery state of charge 0.0 %, "EKF health & home lock: Not armable, no home lock"); the fleet stayed `STAGED`.
- PX4's own refusals: `commander arm` through PX4's own shell client was sent about every 0.3 s from the start of the stack (52 probes
  in total). Recorded: the first probe (0.02 s) got "PX4 server not running", the next ones exit code 1 with the command's usage text, and the
  last ones exit code 0 with no output (the full list of 52 was not stored; the meaning of the exit codes is not verified). PX4's console shows **18 `Arming denied:
  Resolve system health failures first`**, preceded by `Preflight Fail: ekf2 missing data`, `system power unavailable`, `No connection
  to the ground control station`, then `Ready for takeoff!`, then the probe's arm accepted ("Armed by internal command"), disarmed at once.
- The GCS preflight was red at 14.81 s and green, with the drone armed by the probe, at 15.85 s. Only state changes were stored, so the
  exact order of "GCS green" and "PX4 accepts `arm`" inside that 1.04 s cannot be told from this record. A normal hover then completed (HTTP 200).
- `cold-start-2` independently shows the same GCS refusals (412 on both, state STAGED) and that the red window is only 1.25 s after the drone appears.

## Not covered

One drone, on the ground only. A denied `land` while airborne was not run, so the fleet staying in LANDING after a refused fleet-wide
land was not observed (it self-corrected here only because PX4 had disarmed). The arming denials for row 2 are PX4 console lines (no per-line
timestamps), not an acknowledgement read by a client. Real-time factor was not measured. The independent client's heartbeat was not needed:
`cold-start-3` ran without it and the preflight still turned green.

## Layout

`<run>/run.json` (context + checks + notes + cadences), `result.json`, `scenario.log`, `timeline.csv`, `audit.jsonl`, `cmd_audit.jsonl`,
`up.log`, `px4_console.tgz`, `px4_ulogs.tgz`, `docker.log.gz`, `cadence.jsonl.gz`. `rig/` has the scripts that produced them
(operator credentials and the signing key are not included; they are local test files). Run one with
`rig/run_one.sh RUNID CASE tara` after setting the paths at the top.
