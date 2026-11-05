# #171 acceptance run on PX4 SITL, `origin/main` d7e7b2e

One drone, real PX4 v1.16.2 + Gazebo Sim 8.15 in the Docker image `taramandal-px4-sitl:v1.16.2`,
driven through the GCS (`SIMULATION` / `px4` / `px4-sitl`). Run on 4 Nov 2026 (UTC), every run in a
brand-new container that was removed as soon as the run finished. Records are saved per run in
the folders next to this file; nothing here is a hardware claim.

**This run is not the whole #171 acceptance.** The issue also requires a screen recording with the
Gazebo window, RViz and the GCS dashboard visible. This host has no display: everything ran headless,
no Gazebo window, no RViz, no recording. **T2 (PX4 SITL + Gazebo) in #134 is therefore not claimable from this
PR.** Harsh signs off the environment and decides what is still owed.

## Result

| Row (issue #171 / brief) | Status | Run(s) | What was measured |
|---|---|---|---|
| Nominal: arm, ~1 m hover, slow land, 3 consecutive passes | **PASS x3** | `nominal-1`, `-2`, `-3` | every step present, peak 0.99 / 1.01 / 0.99 m, landing peak descent 0.65 / 0.74 / 0.73 m/s (PX4 `MPC_LAND_SPEED` 0.7), ON_GROUND + disarmed, GCS agrees; 19 to 20.5 s end to end |
| Manual takeover mid-hover | **PASS** | `takeover-dwell` | method: MAVSDK MANUAL_CONTROL stream, then `start_position_control()` (OFFBOARD to POSCTL). Sequence stopped 0.14 s after the switch with 500 "operator took over"; PX4 stayed POSCTL and armed for 15 s; the GCS sent no LAND |
| Manual takeover during the landing (#233 B-1) | **PASS** | `takeover-landing` | GCS had commanded LAND at 3.9 m; after the switch PX4 stayed POSCTL, armed, airborne for 25 s; 500 "took over during the landing"; no second land |
| Link loss mid-hover (stop the PX4 process) | **PASS** | `link-kill` | PX4 SIGKILL: `connected` 0 after 0.26 s, `armed` 0 and `armed_stale` 1 at the same time; hover fails closed, 500 "disconnected during hover dwell" |
| Stale telemetry | **PASS, via the rows above and below** | `link-kill`, `stream-drop`, `armed-silent-2` | heartbeat/stream staleness drives `connected` to 0 (0.26 to 3.1 s depending on the fault). A fault where the link stays up but a field freezes was not injected separately |
| Rejected command: hover while preflight is red | **PASS (one route)** | `reject` | PX4 put in LAND mode on the ground: 412 "pre-arm gates are red", message names LAND and the fix, PX4 stayed disarmed; after restoring HOLD a hover completes. **The cold-start version was not exercised**: preflight was already green when asked |
| Rejected `land` (PX4 denies it) | **NOT RUN** | `reject` (partial) | PX4 accepted LAND in every state reachable here (`POST /api/drone/0/land` disarmed on the ground: 200, `commanded: true`), so a PX4-side denial could not be provoked. Covered only by the unit test |
| Abort and land mid-climb | **PASS** | `abort-climb` | `POST /api/land` at 0.65 m: peak 1.69 m (target 3 m), landed and disarmed, hover returns 500 "yielding to fleet emergency state LANDING" |
| Reconnect | **PASS on the GCS side, with a failed first attempt** | `reconnect` (FAIL), `reconnect-settled` (PASS) | PX4 frozen 8 s on the ground: `connected` 0 after 2.5 to 2.8 s, back 0.25 s after resume. See Finding 1: the first attempt armed too soon after the resume and its hover failed closed; the second waits for PX4's estimate to settle (18.5 s) and the hover completes |
| Second hover request during a hover | **PASS** | `guard` | 409; an upload (choreographer and commander tokens) during the hover also 409; the original hover completed; the same upload afterwards is accepted |
| Cancelled request | **PASS (client disconnect only)** | `cancel-2` (`cancel` superseded) | socket closed mid-hover: sequence continued, a new request got 409, PX4 landed and disarmed 15.1 s later, GCS `armed` followed 0.2 s after, and a new hover was then accepted and completed. The in-process `CancelledError` path cannot be triggered from outside; unit test only |
| Dead GCS mid-hover | **PASS** | `gcs-kill` | GCS SIGKILL: PX4 left OFFBOARD for POSCTL after 1.75 s, ON_GROUND and disarmed after 6.8 s. The orphaned `mavsdk_server` was gone (defunct). Stack launched outside the `tara up` supervisor, see Method |
| Dropped telemetry stream | **PASS** | `stream-drop` (ground), `stream-drop-hover` (mid-hover) | `mavsdk_server` SIGKILL: `connected` 0 at once, back after 4.1 s (ground) / 4.3 s (hover); a hover after the recovery completes. Mid-hover: 500 "disconnected during hover dwell", PX4 left OFFBOARD after 2.0 s and was disarmed on the ground after 7.9 s |
| Armed-but-silent drone (#233 B-2) | **PASS** | `armed-silent-2` (`armed-silent` superseded) | armed on the ground, PX4 frozen: `armed` 0, `armed_stale` 1, `armed_stale_ids [0]` after 3.0 s; after resume `armed` 1 / `armed_stale` 0 within 0.26 s. See Finding 2 for what PX4 did after the resume |
| Screen recording, Gazebo + RViz + dashboard | **NOT RUN** | none | headless host, no display |

Runs that failed a check and why (kept, not deleted): `reconnect` (Finding 1), `cancel` (my check ran 0.13 s after PX4's disarm,
before the GCS telemetry caught up; superseded by `cancel-2` with a poll and a stronger guard-release proof),
`armed-silent` (all five substantive checks passed; my cleanup `disarm()` came while PX4 reported IN_AIR and was
denied; superseded by `armed-silent-2`, which waits first). The scenario scripts were fixed between those runs and nothing else changed.

## Measured cadences (the GCS's own MAVSDK connection to udp 14540, all 18 runs)

| Stream | Undisturbed runs (no induced freeze or restart) | Notes |
|---|---|---|
| `landed_state()` | **periodic, 1.00 Hz**: median interval 0.9999 to 1.0003 s, p99 at most 1.034 s, largest gap 1.05 s | 18 to 20 consecutive identical emissions in each nominal run (up to 33 in others), and the landing confirmation still completed: a steady ON_GROUND does not trip the 2.5 s staleness check. No run completed on a stale value |
| `position_velocity_ned` | **about 30 Hz** (29.8 to 30.0), median 0.0322 to 0.0328 s, p99 at most 0.044 s, largest gap 0.10 s | the heartbeat source on hardware |

Gaps over 2.5 s appear only in runs where I froze PX4 or killed `mavsdk_server` (`reconnect*`, `armed-silent*`, `stream-drop*`: 3.5 to 11 s,
each matching the induced fault). Per-run figures are in each `run.json`; the raw arrival times are `cadence.jsonl.gz`.

## Environment (for Harsh to sign off)

- Commit `d7e7b2eb25af2f3e46db9156719089909e978f5a` (`origin/main` at the start of the task), unmodified. Not hand-patched: a fresh container has no `/fs/microsd`, so the upload path creates it.
- PX4 `v1.16.2`; Gazebo Sim 8.15.0; image id `sha256:424c46481374...d92d3cb`; Docker 29.8.0 (linux/amd64); host kernel 7.0.0-38, 16 CPUs.
- Headless, `tara.toml` as in `rig/tara.toml`, signing on (`--allow-unsigned` only in `guard`, which uploads an unsigned file).
- **Host load was not idle for most runs**: 1-min load average 2.6 at the first run, 3.8 to 17 before the others, the driver did not wait between runs, so part of it is my own previous run decaying; it reached 17 around `cancel-2`, and I did not establish whether anything else on the host contributed. Per-run values are in each `run.json`. **Gazebo's real-time factor was not measured.** Nothing in the passes depends on a tight deadline, but it is not a quiet-host claim.
- No other container was running when each run started (`other_running_containers` is empty in every record); I did not check during runs.
- GCS cadence logging: `rig/cadence_sitecustomize.py` was dropped untracked into `packages/tara-gcs/` for each run and removed afterwards. It only timestamps two MAVSDK generators and passes every item through; no tracked file was changed.
- Ground truth for the vehicle is an independent MAVSDK client on PX4's GCS port 14550, not the GCS. It reads PX4's own estimate, not Gazebo's model pose.

## Method

- `./tara up --backend px4 -n 1` for every run except `gcs-kill`. For `gcs-kill` the fleet (`launch_fleet.sh -n 1 -h`) and the GCS (`uvicorn tara_gcs.main:app`, same environment `tara up` gives it) were started separately,
  because `process.py` stops the whole stack when `gcs-api` exits, which would take PX4 down with it and test nothing. That supervisor behaviour is from reading the code; I did not run it.
- Link loss uses the issue's method, stopping the PX4 process, as SIGKILL mid-flight. **SIGSTOP of PX4 is used only on the ground** (reconnect, armed-silent) and corrupts PX4's state, see the findings.
- Each scenario writes explicit pass/fail checks (`run.json`), a 5 Hz position/velocity timeline, the observer's event log, the GCS audit and command-audit logs, the PX4 ULog and console log (`px4_ulogs.tgz`, `px4_console.tgz`) and the raw cadence data.

## Findings

No issues were opened and no product code was changed; software findings go to #131 / #145 as separate PRs if Harsh wants them.

1. **PX4's estimate is wrecked after a SIGSTOP/SIGCONT, and the GCS preflight did not notice (`reconnect`, `reconnect-settled`).** After resuming a PX4 that had been frozen 8 s on the ground, PX4's own altitude estimate read up to 5.7 m in the first run and 123 m with a phantom -44 m/s in the second, on a disarmed vehicle that never moved; it took about 18 s to settle. In `reconnect` the preflight went green again
   while the estimate was still decaying (1.09 m, 0.37 m/s), the sequence armed, and the hover stopped 1.2 s later with 500 "drifted outside the hover altitude band (observed 0.59 m, target 1.0 m)". It failed closed (the GCS commanded land, the safe-recovery disarm was denied by PX4), but the preflight gates look at EKF health, not at whether the estimate agrees with a vehicle on the ground.
   Caveat: a real radio link loss does not freeze the autopilot's clock, so this is mostly a property of the injection under lockstep SITL. The gate gap (green while the estimate is far off) is real and for Harsh to judge.
2. **`armed-silent*`: PX4's state after the resume is also not representative.** In `armed-silent-2` PX4 came back reporting mode `UNKNOWN` and `IN_AIR`, then disarmed itself 8 s after the resume (before my script's `disarm()`); in the first attempt `disarm()` was denied while it reported IN_AIR. The GCS-side result (`armed_stale`) is what this row tests, and that was clean in both.
3. **PX4 reports OFFBOARD again at the moment it disarms after a failsafe** (`gcs-kill`, `stream-drop-hover`: POSCTL, ON_GROUND, disarmed, then OFFBOARD at the same timestamp). Presumably PX4 falling back to the last requested mode; not investigated, and I did not fly again afterwards to see whether it matters.
4. **After an abort the fleet state read LANDING and `armed` 1 at the moment I sampled it** (`abort-climb`, taken right after the sequence returned, while PX4 was already disarmed). I did not record when or whether it returned to STAGED, and did not request a hover after the abort.
5. **Two different speeds for "connected" dropping**: 0.26 s after the PX4 process was killed (`link-kill`) and 2.5 to 3.1 s when PX4 was frozen (`reconnect`, `armed-silent`), consistent with a connection-state change versus a heartbeat timeout. Both are inside the 6 s my checks allowed.

## Not covered

PX4-denied land; hover requested before the first green preflight; the real `CancelledError` path; a fault where the link is up but one telemetry field freezes; the `tara up` supervisor behaviour when `gcs-api` dies (code only); more than one drone; the recording, Gazebo window and RViz;
Gazebo ground truth (PX4's estimate is used); real-time factor.

## Repeat it

`rig/` holds the scripts (`scenarios.py`, `run_one.sh`, `drive_all.sh`, `drive_extra.sh`, ...). They expect a scratch directory mounted at `/scratch` containing `lib.py`, `observer.py`, `scenarios.py`,
`mk_record.py`, `up.sh`, `up_decoupled.sh`, `tara.toml` (committed in `rig/`), an `operators.json` with a `cmdr` (FLIGHT_COMMANDER) and a `chor` (CHOREOGRAPHER) operator, and `studio.pem`; the last two are not committed.
`run_one.sh RUNID CASE [tara|decoupled] [unsigned]` flies one case in a fresh container and removes it afterwards. Cases: `nominal takeover_dwell takeover_landing link_kill reconnect reconnect_settled reject cancel gcs_kill stream_drop stream_drop_hover armed_silent guard abort_climb`.
