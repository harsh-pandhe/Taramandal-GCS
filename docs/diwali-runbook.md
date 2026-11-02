# Diwali demo-day runbook (8 Nov 2026)

Under #122. Written 2 Nov 2026 on `main` at `7a6ab70`. Every command below was run by the author on that
tree unless it is marked **NOT VERIFIED**. A number is only quoted if a run produced it, and the run is named.

Two rules run through this whole document:

1. **Never present a lower tier as a higher one.** Every clip, slide and sentence carries its tier (section 1).
2. **A run on a busy machine is not evidence.** Gazebo's real-time factor falls with host CPU, and a foreign
   job once dropped a run to 0.4. Check the machine first (section 2.1); if a run stalls, aborts or fires RTL,
   rerun it and do not count it (section 5).

## 1. What we can show, and what we cannot

| # | Demo | Tier (say this out loud) | State on 2 Nov | Evidence |
|---|---|---|---|---|
| A | Studio **full-screen preview**, 35 and 100 drones | **Plan playback.** The designed trajectory drawn in 3D. No physics, nothing flew. Shown next to T1, but it is not itself a flight. | Works | Section 2.4 |
| B | `tara_sim`, 35 drones, driven through the real GCS (signed upload, pre-arm latch, launch, onboard start, land) | **T1**, kinematic simulator. GCS header reads `MODE: SIMULATION · tara_sim (kinematic)` and `/api/status` says `fidelity: kinematic`. | Works | Section 2.3, runs below |
| C | `tara_sim`, 100 drones | **T1**, but **not through the GCS dashboard**: the GCS refuses the 100-drone file (section 2.3). Flown by `tools/diwali_sim_scale.py`, which calls `DroneManager` directly. | Works, with the limits stated | Section 2.3 |
| D | Gazebo flight, 3 to 8 drones, through the GCS | **T2**, PX4 SITL v1.16.2 + Gazebo Sim 8.15 in a container. Real flight dynamics, still simulated. `/api/status` says `fidelity: px4-sitl`. | Passed 5 of 5 baseline runs (PR #222, still open); **not runnable on this laptop without Docker and the image** | Section 2.5 |
| E | Real one-drone hover | **T3**, hardware | **Not scheduled and not a given.** See below | none |
| F | 500 drones | T1 | Ring and sphere only, 0.78 to 0.84 real time. Do not show as the greeting | #212, #216 |

**What we cannot show, said plainly**

- **No T3 anywhere.** On this build `/api/status` reports `hardware_flight_certified: false` and
  `hardware_flight_available: false` (read from a running GCS today). #135, the gate for a real hover, is
  deferred behind the Gazebo acceptance and needs Harsh's sign-off on the airframe, bench checks, a pilot with
  RC takeover and a permitted site. The firmware is also undecided: `/api/mode` lists an ArduPilot FMUv2
  hardware profile, while the single-drone hover path uses MAVSDK offboard, which is PX4-only. **Unless #135
  passes and Harsh says so, the demo is simulation only, and every clip says "simulation".**
- **No readable lettering at 35 drones**, marginal at 100 (`examples/diwali/README.md`). The greeting is
  figures (diya, rangoli, ring, sphere), not words, at 35.
- **No 100-drone show through the GCS dashboard** (limits in 2.3).
- **No Gazebo window and no RViz from the current rig** (section 4.2). T2 evidence today is the run record and
  its CSVs, not a recording.
- **No Gazebo above 8 drones.** On this machine 9 drones ran at 0.84 real time and 10 at 0.65 (PR #213 table C,
  stand-in show, before the F1 fix was merged). Show 3, 5 or 8.
- **The first seconds of a T2 show include a descent** until #220 merges (finding F3, below).

## 2. Commands, from a clean machine

All paths are from the repo root. `tools/diwali_*` are new in this PR and add no product code.

### 2.1 Before anything: is the machine quiet?

```bash
docker ps --format '{{.Names}}' | grep '^tara-sitl-' ; echo "(nothing above = no SITL running)"
tools/diwali_cpu_check.sh          # exit 0 = idle; exit 1 = busy, lists the busiest processes
tools/diwali_cpu_check.sh --wait   # block until idle
```

Idle means no `tara-sitl-*` container and a 1-minute load average under 4 (override with `LOAD_MAX`). The
T2 script refuses to start otherwise. On 2 Nov this laptop sat at a load of 20 to 28 for hours from unrelated
jobs (other containers, `pytest`, an `ollama` server), so **the T2 runs of the day could not be taken**
(section 2.5). Expect to have to ask for the machine.

### 2.2 One-time setup (key, operator, config)

```bash
export VIRTUAL_ENV=$PWD/.venv          # or any venv with requirements.txt installed
./tara doctor                          # T1 needs every [OK]; on a laptop the PX4 and gz lines FAIL, and that is expected
D=$HOME/tara-demo; mkdir -p $D && chmod 700 $D
cp tara.toml $D/tara.toml
./tara keygen $D/studio.pem --signer "Diwali demo"       # prints the trusted_keys line
# edit $D/tara.toml: paste that line into [security].trusted_keys, and set backend = "sim"
PYTHONPATH=packages/tara-gcs:packages/tara-core python -m tara_gcs.auth add demo --role flight_commander
#   (prompts for a passphrase of at least 12 characters; writes the operators file owner-only)
```

Point the stack at those files (this is what `$D/env.sh` held when tested):

```bash
export GCS_CONFIG_PATH=$D/tara.toml STUDIO_SIGNING_KEY=$D/studio.pem
export GCS_OPERATORS_PATH=$D/operators.json STUDIO_VAULT_DIR=$D/vault
export GCS_STATE_PATH=$D/state.json GCS_AUDIT_PATH=$D/audit.jsonl GCS_COMMAND_AUDIT_PATH=$D/cmd_audit.jsonl
```

- The Studio backend signs every export with `STUDIO_SIGNING_KEY`; the GCS accepts it because that key is in
  its `trusted_keys`. A key not in `trusted_keys` is refused with 403.
- **NOT VERIFIED on a truly clean machine:** `python3 -m venv .venv && .venv/bin/pip install -r
  requirements.txt` and `npm install` in `packages/tara-web` and `packages/tara-studio-web`. The tested tree
  already had both. `./tara doctor` checks Node (22.23.3 here) and the `node_modules` folders.

### 2.3 T1 (kinematic): 35 drones through the GCS, 100 drones through the scale tool

```bash
# 1. Build and certify the shows through the real Studio /api/export (own backend on :8011, stopped afterwards)
DIWALI_SIZES="35 100" tools/diwali_run_all.sh $D/dist      # about 1 min for 35 + 100
#    -> $D/dist/diwali_35.json, .tara, .result.json (flight export, preview_only false, 0 violations, min sep 2.05 m)
#    The builds are byte-identical to the ones in PR #212 (checked with cmp for 35 and 100).

# 2. Start the stack (Ctrl+C stops it). Dashboard http://localhost:5173, Studio http://localhost:5174
./tara up --backend sim -n 35

# 3. In a second terminal: signed CSV export from the running Studio, then fly it
python tools/diwali_export_csv.py $D/dist/diwali_35.json     # writes diwali_35.csv + .csv.sig.json (1.96 MB, 73,360 rows)
TARA_PASSPHRASE=<the passphrase> python tools/diwali_t1_run.py $D/dist/diwali_35.csv --operator demo
```

`diwali_t1_run.py` makes the same API calls the dashboard buttons make (signed upload, mission upload, the
two-step pre-arm latch, launch, onboard start, land) and prints each state. It exits 0 only if the fleet reached
`SHOW_COMPLETE` with no aborted drone and no RTL/LANDING/ESTOP transition, then landed. **The dashboard buttons
themselves (upload form, PRE-ARM CHECK & TAKEOFF) were NOT clicked by the author; the dashboard was only
watched while this script flew.** Rehearse the click path once before recording.

**Why CSV:** the GCS refuses an upload over 5 MB and a CSV over 100,000 rows, and accepts only `.json`, `.csv`
and `.skyc` (not `.tara`). The 35-drone JSON is 9.4 MB (refused), the CSV 1.96 MB (accepted).
**100 drones cannot go through the GCS in any format:** JSON 45.6 MB, CSV 9.9 MB and 352,600 rows. Tested:
`POST /api/upload-trajectory` returned `400 File too large. Maximum size is 5MB.` Raising the limit is a product
change and needs Harsh. Until then, 100 drones is flown like this:

```bash
python tools/diwali_sim_scale.py $D/dist/diwali_100.json --out $D/sim_100.json     # about 8 min, 80 s of it launch stagger
```

**Expected numbers (T1)**

| | 35 drones | 100 drones |
|---|---|---|
| Certified show | 209.5 s, min sep 2.05 m | 352.5 s, min sep 2.05 m |
| Flown today (this tree) | `SHOW_COMPLETE` after 211.9 s wall (load 4.6 at start), 213.2 s (load ~27), 213.8 s (load not recorded); none aborted, all landed | real-time factor **0.936**, flown min sep after settling 2.042 m, settled after 14.3 s, 2.1% of 10 Hz control ticks missed, 100 of 100 on the ground at the end |
| Earlier, same code family | RTF 0.98 (#212), 0.963 (#216) | RTF 0.927 (#212), 0.929 (#216) |
| Launch to `FORMATION_HOLD` | **40 s** at load 4.6, **221 s** at load ~27, 42 s (load not recorded) | 80 s stagger in the tool |

- The RTF gap at show scale comes from the 5 s show-state snapshot, not the physics (PR #216). The fix is not
  approved or merged. 0.93 to 0.94 at 100 is what the demo will show.
- At 100 drones the launch parks the fleet on hover layers up to 120 m, and the show starts on the pad, so the
  first ~15 s are a descent through the show volume (tracking error up to 120 m before settling). **Start the
  100-drone recording after the fleet has settled**, or say what it is.
- Also seen once, at load ~27: the GCS "Trajectory deviation" panel read 35 of 35 drones CRITICAL, max error
  8.66 m. At load 4.6 the same show read 0.07 to 0.10 m at t+15, 40, 80, 130 and 190 s. I did not establish that
  load is the cause (the earlier capture time into the show was not logged). Do not record with a red panel.

### 2.4 Studio full-screen preview (35 and 100 drones)

```bash
# Studio and the stack running as above. Put the certified show's project into the Project Vault:
node tools/diwali_to_vault.mjs --storyboard examples/diwali/storyboard_35.json  --show $D/dist/diwali_35.json  --name "Diwali 2026 (35 drones)"
node tools/diwali_to_vault.mjs --storyboard examples/diwali/storyboard_100.json --show $D/dist/diwali_100.json --name "Diwali 2026 (100 drones)"
```

Then in the browser at `http://localhost:5174` (driven by Playwright at 1440x900 and checked on screenshots):
open the project, **press GENERATE SEQUENCE first**, wait for "Built" and "Safety passed · export open",
click **FULL-SCREEN PREVIEW**, press Play. `Esc` leaves full screen.

- A freshly opened project shows a red **Refused** badge and a 0.00 s timeline until you press Generate
  Sequence (seen at 35 drones; the tooltip read "cannot be made collision-free: two drones would have to swap
  their vertical order in the same column"). Do this before the camera rolls.
- The preview is **rebuilt by the Studio UI**, so its length differs a little from the certified file: 209.8 s
  vs 209.5 s at 35, 354.6 s vs 352.5 s at 100. It is the same storyboard, not the certified file.
- Measured: page load ~11 s; Generate Sequence 4.2 s (35) and 3.1 s (100) to a built show; at 4x speed the 35
  show played 203.7 s of its timeline in about 52 s of wall time. No console errors at either size.
- Playwright used software WebGL, so the frame rate of a real GPU session is **NOT VERIFIED**.

### 2.5 T2 (PX4 SITL + Gazebo): 3 to 8 drones

This laptop has no PX4 build and no Gazebo (`./tara doctor` fails both), so T2 runs inside the image
`taramandal-px4-sitl:v1.16.2` (PX4 v1.16.2, Gazebo Sim 8.15; present locally, 7.6 GB). Docker Desktop's
`--network host` is the VM's network, so the GCS and the measuring tool run inside the container.

```bash
# Docker Desktop (Linux): after a reboot the daemon is down. Verified: this brought it back in ~25 s.
systemctl --user start docker-desktop
docker info --format '{{.OperatingSystem}} {{.NCPU}} cpus'      # Docker Desktop, 16 cpus, 3.9 GB RAM

tools/diwali_cpu_check.sh --wait
VENV=$PWD/.venv tools/diwali_t2_sitl.sh 3 myrun examples/diwali/segments/diwali_3drone_segment.json
# 5 and 8 drones: build them from the same storyboard against a running Studio (`./tara up --backend sim -n 5`), then pass the file
node tools/diwali_build.mjs --storyboard examples/diwali/storyboard_3.json --n 5 --tag n5 --api http://127.0.0.1:8001 --out $D/dist5
VENV=$PWD/.venv tools/diwali_t2_sitl.sh 5 myrun5 $D/dist5/diwali_n5.json
```

The script starts a fresh container each run (so a missing mission directory cannot hide), copies Node in (the
image has none), starts `./tara up --backend px4 -n N`, waits for N drones, then runs
`tools/sitl_planned_vs_actual.py` (PR #213), which certifies, signs, uploads, launches, starts the show
(`execution="streamed"`, since stock PX4 has no onboard executor), samples Gazebo's own poses and writes a
verdict. Results: `~/tara-sitl-demo-scratch/runs/<label>.json` and CSVs. Stop it: `docker rm -f tara-sitl-demo`.

**What the author verified of this script**

- Refuses while any other `tara-sitl-*` container runs, and `FORCE_BUSY=1` does not override that (tested
  against another tab's container); refuses on a busy CPU unless `FORCE_BUSY=1` (its load comparison had a rounding bug, fixed in this PR: 3.63 was read as 4).
- **One complete run through the script, 3 drones, the 3-drone segment, on this PR's tree (`a10c168`, plus the uncommitted load-check fix below):
  PASS by the tool's rule.** Host load average 3.62 at the start (under the gate's 4). Stack up 3 of 3 in 24 s;
  3 of 3 missions verified; launch to `FORMATION_HOLD` 10.7 s; playback 84.23 s of 84.2 s planned; Gazebo min
  separation **2.192 m** (Studio 2.20); real-time factor **0.997**, no stall; start transient 1.38 / 2.56 /
  3.57 s per drone and max error 5.90 m (the F3 start dive); landing errors 0.019 / 0.037 / 0.024 m; no abort,
  no RTL, final state `DISARMED`. Load was 11.3 at the end, and I did not log it during the flight.
- **One run is not a set.** The earlier forced attempts on a loaded host (load ~19 to 27) stopped at the launch
  gate ("drone 2 holds a mission but is disconnected"), so a busy host can break the run before it starts.
  **NOT VERIFIED:** 5 and 8 drones through the script, and three consecutive 3-drone runs. #222's five baseline
  runs were taken with the equivalent hand-made rig scripts, which are not in the repository.
- The 5- and 8-drone builds certify (84.8 s and 87.4 s, min sep 2.20 m, 0 violations). I did not diff them
  against the files #222 flew.

**Expected numbers (T2): PR #222, flown on `7a6ab70`, still open; all five runs PASS by the tool's rule**

| Drones | Runs | Studio min sep | Gazebo min sep | Real-time factor | Start transient | Max error incl. start dive | Worst landing error |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 3 | 3 | 2.20 m | 2.147 to 2.169 m | 0.993 to 1.000 | 3.5 to 3.7 s | 5.91 to 5.98 m | 0.044 to 0.083 m |
| 5 | 1 | 2.20 m | 2.152 m | 0.980 | 5.52 s | 9.93 m | 0.054 m |
| 8 | 1 | 2.20 m | 2.106 m | **0.913** | 7.67 s | 15.91 m | 0.048 m |

- **These include the start dive (F3).** The F3 fix is #220, open and needing Harsh's approval. If it merges,
  the three-run set has to be repeated; until then say "a descent in the first seconds is expected".
- Pass rule (`docs/sitl-planned-vs-actual.md`): certified, signed, started streamed; `SHOW_COMPLETE` with no
  aborted drone and no RTL/ESTOP/LANDING; Gazebo separation at or above 1.5 m; all disarmed; sim live with no
  gap over 2 s. A pass at real-time factor under 0.9 is marked degraded.
- The Gazebo separation (2.1 to 2.17 m) is slightly under Studio's 2.20 m. Quote the Gazebo number, not Studio's.
- 8 drones at 0.913 is near the edge. Prefer 3 or 5 for anything that must look smooth.

## 3. Where the files are

| What | Where |
|---|---|
| Storyboards (35, 35+lettering, 100, 500, 3) | `examples/diwali/storyboard_*.json`, SVGs in the same folder |
| Figure previews | `examples/diwali/previews/` |
| 3-drone segment (T2), unsigned | `examples/diwali/segments/diwali_3drone_segment.json` and `.tara` |
| Measured T1 results from #212 | `examples/diwali/results/` |
| Build, certify, sim scale | `tools/diwali_run_all.sh`, `tools/diwali_build.mjs`, `tools/diwali_sim_scale.py` |
| Demo helpers (this PR) | `tools/diwali_cpu_check.sh`, `diwali_export_csv.py`, `diwali_t1_run.py`, `diwali_to_vault.mjs`, `diwali_t2_sitl.sh` |
| T2 planned vs actual tool and method | `tools/sitl_planned_vs_actual.py`, `docs/sitl-planned-vs-actual.md` |
| Built shows | not committed (45 MB at 100 drones); rebuild is deterministic |
| Signing key, operators file, passphrase | `~/tara-demo/`, owner-only, **never committed** |

## 4. Recording checklist

### 4.1 Before recording

- [ ] `tools/diwali_cpu_check.sh` says IDLE, and nothing else is flying on Gazebo.
- [ ] `git rev-parse --short HEAD` noted; the clip's title card or filename carries it.
- [ ] Stack restarted fresh (a finished run leaves the fleet `DISARMED`, and `diwali_t1_run.py` insists on `STAGED`).
- [ ] Studio project open, **Generate Sequence pressed**, badge reads Built and Safety passed.
- [ ] GCS signed in; header shows `MODE: SIMULATION · tara_sim (kinematic)` (T1) or the px4-sitl label (T2).
- [ ] The deviation panel is green (see 2.3). No red banner in frame.
- [ ] Window exactly **1440x900**.

### 4.2 What must be visible, and the limits of this rig

The ask is Gazebo, RViz and the GCS dashboard together.

| Pane | Who and how | State |
|---|---|---|
| GCS dashboard (ops, Earth, Night Sky) | Dhiraj, `http://localhost:5173` | Verified in a browser; a clean 1440x900 capture works (below) |
| Studio full-screen preview | Vishal (Studio owner), `http://localhost:5174` | Verified (2.4) |
| **Gazebo** | The container is **headless**, and Docker Desktop's VM has no display path to the host (`docs/sitl-planned-vs-actual.md`). No Gazebo window exists on this rig. | **NOT VERIFIED anywhere.** Needs a Linux machine with PX4 v1.16.2 and Gazebo Sim 8 installed natively, `./tara up --backend px4 -n 3 --gui`. This laptop has neither. |
| **RViz** | **Harsh** (owns the #171 recording). It is not in the container: the image has no ROS 2. `sim/ros/launch_rviz.sh` needs ROS 2 Jazzy, `ros_gz_bridge`, `rviz2`, a display, and the same Gazebo transport partition as the fleet. | **NOT VERIFIED.** No ROS 2 here; across the Docker Desktop VM boundary the partition is not reachable. The script's header still names `scripts/launch_fleet.sh`, which no longer exists. |

So, until Harsh names a machine, **a three-pane Gazebo + RViz + dashboard recording is not possible, and the
T2 clip can only be the dashboard plus the run's CSV-based plot.** Do not splice a Gazebo screenshot from
elsewhere into a clip and call it this run.

Browser-only capture at exactly 1440x900 works: Playwright `recordVideo` with a 1440x900 viewport produced a
valid `.webm` (checked: file type WebM). The laptop's own display is 1920x1080 with no 1440x900 mode in
`xrandr`, so a whole-screen recording is the wrong size; use a 1440x900 window or region. GNOME's built-in
screen recorder and `ffmpeg` were **NOT VERIFIED** (`ffmpeg` is not installed here).

### 4.3 Naming and storage

`diwali2026_<tier>_<N>drones_<what>_<YYYYMMDD>_<sha7>.<ext>`, for example
`diwali2026_T1_35drones_gcs-dashboard_20261105_7a6ab70.webm` and
`diwali2026_plan_100drones_studio-fullscreen_20261105_7a6ab70.webm`. Tier is one of `plan`, `T1`, `T2`.
There is no `T3`. Next to every clip keep its `<label>.json` run record (T2) or the terminal log (T1).

- **Not in git.** Clips are large, and the evidence pack says it must not contain a simulated recording
  (`docs/evidence-pack.md`, section 8).
- Working copy: `~/diwali-recordings/` on the recording machine. **The shared location does not exist yet and is
  Harsh's decision.** Nothing in the repository or the evidence pack names one.
- No music has been chosen and no licence recorded (#132). A silent clip is the default.

## 5. Who does what, abort and rollback

### 5.1 Who does what

Roles are from the 1 Nov ownership split and the issues; the recording split is **proposed, for Harsh to confirm**.

| | Harsh | Vishal | Dhiraj |
|---|---|---|---|
| Decisions | Storyboard sign-off; whether #135 is attempted; firmware; approves #220 and any fix to the upload limit or the snapshot; the shared recording location; music rights | Studio calls | none |
| Review | Super-review before every merge; frozen from Sat 7 Nov | Reviews #216, #219, #222 and this PR; Studio PRs | Does not merge |
| Studio side | | Full-screen preview and operator UX (#132); Studio walkthrough recording (proposed) | Shows are built and certified |
| GCS, sim, Gazebo | | | T1 and T2 runs, run records, evidence-pack rows; #219, #220 |
| Hardware / RViz / Gazebo window | #135, RViz, the machine with a display (proposed) | | Supplies the commands in this document |

### 5.2 Abort and rollback

Fleet commands (`/api/rtl`, `/api/land`, `/api/estop`, and per drone `/api/drone/<id>/rtl|land|estop`) are the
same as the dashboard's EMERGENCY RTL, EMER LAND and E-STOP (KILL). E-STOP kills motors; airborne drones fall.
**E-STOP was NOT tested by the author.**

| Event | What it looks like (verified where stated) | What to do |
|---|---|---|
| **One drone aborts** | Verified on T1, 35 drones, 25 s into the show: `POST /api/drone/7/rtl` returned 200; the GCS modelled drone 7's RTL path passing **0.73 m from drone 9** and 2.00 m from drone 11, and ~3 s later the proximity monitor fired `failsafe:proximity` and **RTL for the whole fleet**. The show ended and the fleet landed. | An abort ends the take for everyone. **Discard the take, restart the stack, rerun.** Do not demonstrate a single-drone abort on camera in a formation. One observation, one time offset; not characterised further. T2 not tested by the author. |
| **RTL fires** | `/api/fleet-state` history names the trigger: `failsafe:proximity`, `failsafe:geofence`, or `POST /api/rtl`. Verified: after `POST /api/rtl` mid-show the fleet reached `DISARMED` within 15 s. | Read the trigger, keep the JSON, **discard the take**. Restart the stack: a finished run leaves the state `DISARMED` and the runner needs `STAGED`. Whether a restart can be avoided is NOT VERIFIED. |
| **Sim stalls** | T2: the tool marks the run DISCARD if Gazebo poses stop for more than 2 s, and marks it degraded under 0.9 real time. T1: the show took more than ~15 s longer than planned, or the deviation panel went red (**15 s is a proposed threshold, not a measured one**). | **Rerun. Do not count it.** First ask whether the machine was idle: `tools/diwali_cpu_check.sh`. |
| **Docker dies** | Verified after a reboot: `docker ps` says it cannot connect to the daemon. `systemctl --user start docker-desktop` restored it in ~25 s; images stayed, containers were gone. | Start Docker Desktop, then rerun in a **new** container (the script does that). A T2 run that lost its container is discarded. T1 does not use Docker and is unaffected. |
| **Stuck stack** | `Ctrl+C` in the `./tara up` terminal stops the stack (verified with SIGINT). | `./tara reset` runs `pkill -9` on `px4`, `mavsdk_server`, `gz sim`, `uvicorn tara_gcs`/`tara_studio` and `vite` on ports 5173/5174. **It will kill another tab's stack on the same machine. NOT RUN by the author for that reason.** Use it only when nothing else is running, or inside the T2 container. `sim/px4/stop_fleet.sh` also kills every `mavsdk_server`, including a running GCS's. |

**Rollback.** The last commit with a full recorded set of T2 passes is `7a6ab70` (PR #222). If `main` changes
after #220 or any other merge and a rerun does not match the section 2.5 table, record from a checkout of
`7a6ab70` and say that the start descent is visible. **Nothing merges on Sat 7 Nov**; the 8 Nov recording uses
the commit that passed the Saturday dry run, and that SHA is in every filename.

## 6. Day by day, 2 to 8 Nov

Only items that exist today. Owners are proposed. Every item that needs a merge needs Harsh's approval first.

| Day | Item | Owner | Depends on |
|---|---|---|---|
| Mon 2 | Review this PR and #222, #219, #220, #216; Harsh decides whether #135 is in play and whether #220 goes in | Vishal, Harsh | PRs exist |
| Mon 2 | Harsh names the machine for the Gazebo window and RViz, or confirms the T2 clip is dashboard plus run record | Harsh | decision |
| Tue 3 | If #220 merged: rerun the 3-drone ×3, 5 and 8 baseline with `tools/diwali_t2_sitl.sh` on an idle machine and compare with 2.5. (The script has one clean 3-drone pass so far.) | Dhiraj | #220, idle machine |
| Tue 3 | Studio preview at 35 and 100: confirm the Generate-first step on a real GPU session | Vishal | none |
| Wed 4 | T1 35 drones through the GCS on an idle machine, three clean runs, logs kept; rehearse the dashboard button path once | Dhiraj | idle machine |
| Wed 4 | 100-drone clip plan: `diwali_sim_scale.py`, start after settling | Dhiraj | none |
| Thu 5 | Recording pass 1: Studio preview 35 and 100, GCS dashboard T1 35, named per 4.3 | Vishal, Dhiraj | 1440x900 window |
| Thu 5 | Recording pass 1: T2 3 drones (Gazebo + RViz + dashboard if Harsh's machine exists, else dashboard + run record) | Harsh, Dhiraj | Mon decision |
| Fri 6 | Retakes of anything that aborted, stalled or went red; evidence-pack rows with the SHA (#134); music decision (#132) or silent | Dhiraj, Harsh | none |
| Sat 7 | **Freeze**: no merges. Full dry run of this document from a stopped machine, top to bottom | Dhiraj | Fri |
| Sun 8 | Demo day. Real hover only if Harsh has signed #135; otherwise the demo is simulation and says so | Harsh | #135 |

## 7. Verification log (2 Nov 2026)

| Item | Result |
|---|---|
| `./tara doctor` | Python, Node 22.23.3, `node_modules` OK; PX4 build and `gz` FAIL on the host (expected) |
| Build and certify 3, 35, 100 | OK; 35 and 100 byte-identical to #212's builds; 5 and 8 certify |
| Signed CSV export, 35 and 100 | OK; 100 is over the GCS limits (warned by the tool, refused by the GCS: 400) |
| T1 35 drones, 3 full runs through the GCS API | 211.9 / 213.2 / 213.8 s, none aborted, all landed |
| T1 100 drones, `diwali_sim_scale.py` | RTF 0.936, settled 14.3 s, flown min sep 2.042 m |
| Studio preview 35 and 100 | OK (Playwright, headless Chromium, 1440x900) |
| GCS dashboard sign-in, Operations, Earth, Night Sky | Rendered; clicks on upload and PRE-ARM buttons not made |
| Single-drone RTL and fleet RTL mid-show (T1) | Fleet-wide proximity RTL after drone 7's abort; fleet RTL reached DISARMED |
| T2 through `diwali_t2_sitl.sh`, 3 drones, one run | PASS: Gazebo min sep 2.192 m, RTF 0.997, landing under 4 cm. 5 and 8 drones, and a 3-run set, **NOT VERIFIED** |
| Gazebo window, RViz, GNOME/ffmpeg recording, E-STOP, clean `pip`/`npm install`, `./tara reset` | **NOT VERIFIED** |

**Overlap disclosure.** Four of the author's runs shared the machine with another tab's `tara-sitl-171fix`
(Gazebo): a headless browser for about 70 s from 04:35; the last ~1 minute of the 05:32 T1 run (the container
appeared at about 05:35); a 5-drone kinematic stack for about 1 minute at about 05:38, started after `docker ps`
had printed that container (the check was only printed, not enforced: an error); and a T2 attempt at 05:37 that
was stopped within seconds. None was Gazebo work, but that tab's real-time factor in those windows should be
treated as perturbed. The T2 script's SITL gate was made non-overridable after the 05:37 attempt.
