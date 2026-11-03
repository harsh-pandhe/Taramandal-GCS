# Dead GCS mid-hover on PX4 SITL (issue #171)

Records for the GCS lifeline fix: when the GCS process dies, its `mavsdk_server`
must die with it, so the offboard setpoint stream stops and PX4's own
offboard-loss failsafe takes over. Ground truth comes from an independent MAVSDK
client on PX4's GCS port (`rig/observer.py`), not from the GCS.

## Method

- Image `taramandal-px4-sitl:v1.16.2` (PX4 v1.16.2, Gazebo Sim 8, headless), 1 drone. The GCS
  runs in the same container (`--network host` under Docker Desktop is the VM's network).
- Mounts: the worktree at `/work`, the host `.venv` at `/venv:ro`, this `rig/` directory plus
  an `operators.json` with a FLIGHT_COMMANDER at `/scratch`.
- `restart_fleet.sh` starts PX4 + Gazebo. `start_gcs.sh 1|0` starts the GCS with or without the
  lifeline (see `gcs_run.py`).
- `TARA_PASSPHRASE=... python kill_case.py SIGKILL 60`: hover to 2.0 m with a 30 s hold, wait
  until above 1.5 m plus 3 s, send the signal to the GCS, then log PX4's mode, armed state and
  altitude every 0.1 s until disarm or the watch window ends. Cleanup lands through the observer.
- PX4 parameters as read in each run: `COM_OF_LOSS_T` 1.0, `COM_OBL_RC_ACT` 0, `COM_RC_IN_MODE`
  1, `NAV_DLL_ACT` 2, `COM_RCL_EXCEPT` 0.

## 2026-11-02, main `7a6ab70`, lifeline injected at launch

Taken with an earlier, terser copy of these rig scripts; same steps and timings. The scripts
were reformatted (passphrase moved to `TARA_PASSPHRASE`) before being committed.

Host load averaged about 10. An earlier attempt at a load of about 30 flapped from the start
and is not counted.

| Record | Signal | mavsdk_server | PX4 leaves OFFBOARD | Disarmed on ground |
|---|---|---|---|---|
| `main-sigkill.log` (no lifeline) | SIGKILL | alive for the whole 60 s | never: OFFBOARD, armed, 2.0 m at +60 s | – |
| `lifeline-sigkill-1.log` | SIGKILL | gone +0.10 s | +0.94 s (POSCTL) | +6.95 s (observer; polling line +7.6 s) |
| `lifeline-sigkill-2.log` | SIGKILL | gone +0.10 s | about +0.9 s (POSCTL) | +7.7 s |
| `lifeline-sigkill-3.log` | SIGKILL | gone +0.10 s | about +0.9 s (POSCTL) | +7.6 s |
| `lifeline-sigterm-1.log` | SIGTERM | gone after the GCS exited | +29.6 s (LAND, commanded by the sequence) | +35.7 s |

How to read these:
- **Mode-change times.** The "PX4 left OFFBOARD" line in a log is the 0.1 s polling loop's
  timestamp, which also runs `pgrep`, so it can lag. The observer's own `OBS mode:` line is the
  better time (+0.94 s in run 1).
- **SIGTERM.** Uvicorn treats SIGTERM as a graceful stop and waits for in-flight requests. The
  hover therefore finished its hold, landed itself and disarmed; only then did the GCS, and its
  server, exit.
- **The failsafe action.** It comes from PX4 parameters, not from the GCS. With
  `COM_OBL_RC_ACT=0` PX4 reports POSCTL; with no stick input in SITL it descended and disarmed.
  On a vehicle with a live transmitter, POSCTL hands control to the pilot instead.
