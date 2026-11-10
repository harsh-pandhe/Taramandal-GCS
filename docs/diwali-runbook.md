# Diwali demo-day runbook (8 Nov 2026)

Under #122. Written 2 Nov 2026 on `main` at `7a6ab70`; revised on 3 Nov after Vishal's review of #225
and after #219, #220, #221, #222, #223, #224 and #225 merged. Every command below was run by the author unless it
is marked **NOT VERIFIED**. A number is only quoted if a run produced it, and the run is named. The T2 numbers in
2.5 were re-taken on `main` at `b435358` (after #220); the T1 numbers in 2.3 and the Studio numbers in 2.4 are
still from `7a6ab70` and were not re-taken.
On 4 Nov the steps that need no SITL were re-run at 3 drones on `origin/main` `d7e7b2e`; see
`docs/diwali-runbook-verification/README.md` (screenshots, what was stubbed, seven findings, none fixed).

**Refreshed on 6 Nov 2026 (Fri) from `origin/main` at `117da30`. Docs only: nothing was run in this pass (no stack,
no build, no browser, no container), so every step below that was not run on the date and commit named next to it
is **NOT VERIFIED in this pass**, and the timeline says so row by row.** What changed since 3 Nov: #240 merged,
so a launch that does not get every planned drone up is now refused by name (2.6); #237 merged, so the figures-only
shows and their real lengths exist (2.3); #244 (PX4-safe pacing) and #245 (voltage format) merged (2.7); four T2 clips and one
T1 clip were made on 5 Nov and are **made, not yet in the repository** (4.4); the dashboard changed (#93) and its
drone-card coordinate formatting landed (52d5e54), neither re-driven here. The RC pilot is still **NOT YET NAMED**
(5.3). The one-page timeline for the day is straight after the two rules below.

**Platform: Linux only.** Everything here was run on Linux. Not supported on Windows: `tools/diwali_to_vault.mjs`
fails there (`ERR_UNSUPPORTED_ESM_URL_SCHEME`: it passes an absolute path to `import()`) and
`tools/diwali_t1_run.py` calls `os.getloadavg`, which Windows lacks. Neither is fixed; do not try this on a
Windows laptop on the day.

Two rules run through this whole document:

1. **Never present a lower tier as a higher one.** Every clip, slide and sentence carries its tier (section 1).
2. **A run on a busy machine is not evidence.** Gazebo's real-time factor falls with host CPU, and a foreign
   job once dropped a run to 0.4. Check the machine first (section 2.1); if a run stalls, aborts or fires RTL,
   rerun it and do not count it (section 5).

## Demo-day timeline (one page)

Sun 8 Nov. Offsets are from going on camera (T0) and are a plan, not a schedule that was ever run end to end.
**Nothing in this table was run in the 6 Nov pass.** The "Last run" column says when and on what each step was
last actually done; a step with no date there has never been run. Times are the ones measured on that run.
Tier for every row: simulation (T1 or T2); there is no hardware step unless #135 passes and 5.3 has a name.

| T | Start / do | Takes (measured) | A failure looks like | First thing to do | Last run |
|---|---|---|---|---|---|
| -60 min | **Is the machine quiet.** `docker ps --format '{{.Names}}' \| grep '^tara-sitl-'`, then `tools/diwali_cpu_check.sh --wait` (2.1). Read the latest slot comment on #172 | seconds | `BUSY:` plus the busiest processes; a `tara-sitl-*` container; "SITL slot taken" | Do not start. Find the owner. A native Gazebo fleet from another session on the same machine also counts (no container shows it): `pgrep -cx px4` | scripts 2 to 3 Nov; the native-fleet case seen 5 Nov |
| -55 | **Clean slate.** `./tara status` must say "nothing running". Only if it lists strays and nothing else of yours or anyone's is running: `./tara reset` (5.2) | seconds | Strays listed; ports 8090, 8001, 5173, 5174 already in use (`address already in use`) | Find who owns the port (`ss -ltnp`). `./tara reset` kills every native stack of the project on the machine, whoever started it | `reset` and `status` 4 Nov (sim stack) |
| -50 | **Start the stack.** T1: `./tara up --backend sim -n 35`. T2: `tools/diwali_t2_sitl.sh` (2.5). Dashboard `:5173`, Studio `:5174` | T1: about 20 s at 3 drones, all 35 connected within about 5 s of the GCS answering. T2: container plus stack, minutes (not timed here) | `connected` below N; a service "exited unexpectedly"; **the stack vanishes mid-run with `received SIGINT`** (5 Nov: another session's launcher cleanup killed three of my runs) | Read the first error in the `tara up` terminal. Never go on with `connected < N`: restart. If it vanishes, discard the take and find what else is running | T1 3 and 35 drones 4 to 5 Nov; T2 3 Nov |
| -45 | **Sign in** to the dashboard. Header `MODE: SIMULATION · tara_sim (kinematic)` (T1) or the px4-sitl label (T2) | seconds | Login 401; header says a different tier; a red banner | Fix the tier before anything else: a clip with the wrong label is discarded (rule 1). One `WebSocket error` in the browser console at sign-in is known and harmless | 4 Nov (before #93; **NOT VERIFIED on the current dashboard**) |
| -40 | **Studio: Generate Sequence first**, then wait for `Built` and `Safety passed · exports enabled` (2.4). Then export the signed show (JSON, or CSV at 35) | Generate 1.3 s at 3 drones, 4.2 s at 35, 3.1 s at 100 | Badge `Not flight-certified`, `Safety failed` or `Unverified`, exports locked | Do not export and do not call it certified. Press Generate again; if it stays locked, switch to the show built and certified by `tools/diwali_run_all.sh` (2.3). **Pacing: use Standard, never Fast (2.7)** | badge states 4 Nov at 3 drones; 35 and 100 on 2 Nov, **NOT VERIFIED since** |
| -35 | **Upload** the show and its `.sig.json` together; the mission stages itself 0.5 s later (2.3) | upload about 4 s and missions verified about 1 s at 35 drones (5 Nov) | `400 File too large` (over 5 MB or 100,000 rows: 35 as CSV fits, 100 never does); `403` key not trusted; "signature not verified"; missions not verified | Read the message; do not use `--allow-unsigned`. Check `trusted_keys` and the file size | 35 drones 5 Nov (API runner); dashboard upload 4 Nov at 3 drones |
| -30 | **PRE-ARM CHECK & TAKEOFF, then LAUNCH FLEET.** On the sim backend there is no latch step (2.3). On PX4 the two-step latch applies and has not been clicked through the dashboard | launch to `FORMATION_HOLD`: 4 s (3 drones T1), 38 s (35 drones T1, three runs), about 19 s (5 drones T2) | `LAUNCH BLOCKED` with red gates; **or the fleet header stays `TAKEOFF` and START SHOW stays greyed** | Red gate: read the reasons, do not bypass. **Stuck in `TAKEOFF`: a planned drone did not get up (2.6): read which one, then EMER LAND and relaunch** | T1 4 to 5 Nov; T2 5 drones 3 Nov and 8 launches in #240 on 4 to 5 Nov |
| -25 | **START SHOW** when the header says `FORMATION_HOLD` (not before) | the show: 84.2 s at 3 drones; at 35 see the two-column length table in 2.3 (207.6 s repo build, 217.9 s built in Studio, figures-only) | `409 launch_incomplete` (API only, the dashboard button is greyed); deviation panel red; a drone drops out | Stop the take. Do not carry on with a drone missing | 3 drones 4 Nov; 35 drones started 5 Nov but no 35-drone show was flown to the end on 4 to 6 Nov (2 Nov: 211.9, 213.2, 213.8 s) |
| during | **Watch the fleet header and the deviation panel.** Do not touch the dashboard | n/a | `failsafe:proximity` or `failsafe:geofence` RTL; drones heading home; deviation panel red; a stall (T1: show more than about 15 s over plan) | Let the RTL finish; do not click. Read the trigger in `GET /api/fleet-state` history. Discard the take (5.2). E-STOP only to stop a flyaway: it kills motors | RTL 2 Nov (T1 35 drones); E-STOP command path 4 Nov (sim only) |
| end | **Land.** After `SHOW_COMPLETE` the only landing control is EMER LAND then CONFIRM LAND? (there is no plain Land) | T1 3 drones: `DISARMED` about 1 s after the command; T2 5 drones: about 6 s. 35 drones: not timed in this pass | Fleet not `DISARMED` within about 15 s; a drone still armed | `POST /api/rtl` is the second try; E-STOP is the last (5.2). On T2, read each drone's tracking error before counting the take (2.5) | 4 Nov (T1 3 drones); 3 Nov (T2) |
| between takes | **Restart the stack** (`Ctrl+C`, `./tara up` again). `diwali_t1_run.py` refuses anything but `STAGED`; the GCS allows `LAUNCH` from `DISARMED` too (read from `fleet_fsm.py`, **NOT VERIFIED** through the dashboard) | about 20 s | Runner says "fleet is DISARMED, not STAGED" | Restart; do not hunt for a way round it on the day | refusal 4 Nov |
| after | **Stop.** `Ctrl+C` in the `tara up` terminal; T2: `docker rm -f tara-sitl-demo`; post "SITL slot free" on #172 if you took it | seconds | Orphans on 5173 to 8090 after a hard kill | `./tara status`, then `./tara reset` only if nothing else is running | SIGINT 4 and 5 Nov |

The Studio full-screen preview (plan playback, section 2.4) needs only the Studio backend and can be shown without
the GCS: open the project, **Generate Sequence first**, FULL-SCREEN PREVIEW, Play, `Esc`. At 3 drones this worked
on 4 Nov; at 35 and 100 it last worked on 2 Nov, before #244 changed the pacing, so **both sizes are NOT VERIFIED
and must be re-driven after #244 merges.**

## 1. What we can show, and what we cannot

| # | Demo | Tier (say this out loud) | State (date and commit in the section named) | Evidence |
|---|---|---|---|---|
| A | Studio **full-screen preview**, 35 and 100 drones | **Plan playback.** The designed trajectory drawn in 3D. No physics, nothing flew. Shown next to T1, but it is not itself a flight. | Works | Section 2.4 |
| B | `tara_sim`, 35 drones, driven through the real GCS (signed upload, pre-arm latch, launch, onboard start, land) | **T1**, kinematic simulator. GCS header reads `MODE: SIMULATION · tara_sim (kinematic)` and `/api/status` says `fidelity: kinematic`. | Works | Section 2.3, runs below |
| C | `tara_sim`, 100 drones | **T1**, but **not through the GCS dashboard**: the GCS refuses the 100-drone file (section 2.3). Flown by `tools/diwali_sim_scale.py`, which calls `DroneManager` directly. | Works, with the limits stated | Section 2.3 |
| D | Gazebo flight, 3 to 8 drones, through the GCS | **T2**, PX4 SITL v1.16.2 + Gazebo Sim 8.15 in a container. Real flight dynamics, still simulated. `/api/status` says `fidelity: px4-sitl`. | On `b435358` (after #220): 3 drones 3 of 3 clean; 5 drones 2 of 3 clean, and **one run left drone 3 on the pad and still said PASS** (not reproduced in 10 repeats on `d7e7b2e`, #243; and #240, merged, now refuses such a launch, 2.6); 8 drones 1 of 1 clean. **Not runnable on this laptop without Docker and the image** | Section 2.5 |
| E | Real one-drone hover | **T3**, hardware | **NOT FLIGHT READY. Not scheduled and not a given.** See below and section 5.3 | none |
| F | 500 drones | T1 | Ring and sphere only, 0.78 to 0.84 real time. Do not show as the greeting | #212, #216 |
| G | **Recorded clips** of T1 (3 drones) and T2 (1, 3 and 10 drones, Gazebo + RViz + dashboard) | Each clip says its own tier. T2 clips come from a **local, non-stock build** (Gazebo 8.10, patched PX4 v1.16.2), not the container image | **Made on 5 Nov, not yet in the repository** | Section 4.4 |

**What we cannot show, said plainly**

- **NOT FLIGHT READY.** Nothing in this repository has flown hardware, and no software on this build can say
  a real vehicle is ready: `GET /api/mode` hard-codes `rc_takeover_available: false` and `rc_status:
  "UNVERIFIED"`, so RC takeover can only ever be confirmed by a person at the bench (section 5.3).
- **No T3 anywhere.** On this build `/api/status` reports `hardware_flight_certified: false` and
  `hardware_flight_available: false` (read from a running GCS today). #135, the gate for a real hover, is
  deferred behind the Gazebo acceptance and needs Harsh's sign-off on the airframe, bench checks, a **named**
  pilot with RC takeover (nobody is named yet, section 5.3) and a permitted site. The firmware is also undecided: `/api/mode` lists an ArduPilot FMUv2
  hardware profile, while the single-drone hover path uses MAVSDK offboard, which is PX4-only. **Unless #135
  passes and Harsh says so, the demo is simulation only, and every clip says "simulation".**
- **No readable lettering at 35 drones**, marginal at 100 (`examples/diwali/README.md`). The greeting is
  figures, not words, at 35. A figures-only fallback exists for both sizes (#237, merged: 35 drones = diya, sphere
  burst, diya, with the rangoli dropped; 100 = diya, rangoli, sphere burst, diya). Which of lettered or
  figures-only is flown is Harsh's choice; no decision is recorded in this repository (NOT VERIFIED whether it was
  made elsewhere).
- **No 100-drone show through the GCS dashboard** (limits in 2.3).
- **No Gazebo window or RViz from the container rig** (section 4.2). The T2 run records and CSVs are the
  evidence from the container. Gazebo + RViz + dashboard clips exist from a separate native build (section 4.4,
  made, not yet in the repository), and each is labelled as a local build.
- **No Gazebo above 8 drones.** On this machine 9 drones ran at 0.84 real time and 10 at 0.65 (PR #213 table C,
  stand-in show, before the F1 fix was merged). Show 3, 5 or 8.
- **A T2 run could say PASS with a drone that never flew.** In a 5-drone run on 3 Nov, drone 3 stayed on the pad
  in `HOLD` for the whole show, the fleet went on without it, and the tool printed `PASS` (its rule has no
  per-drone tracking check, and **that is still true**). It did not repeat in the next two runs nor in 10 repeats
  on `d7e7b2e` (#243), and the cause is not known (section 2.5). Since #240 (merged) the GCS itself refuses to
  reach `FORMATION_HOLD` with a planned drone missing (2.6; SITL-checked on 4 to 5 Nov, 8 launches), but the
  planned-vs-actual tool is unchanged: read every drone's tracking error in the run record before counting any T2
  run.

## 2. Commands, from a clean machine

All paths are from the repo root. `tools/diwali_*` are new in this PR and add no product code.

### 2.1 Before anything: is the machine quiet?

```bash
docker ps --format '{{.Names}}' | grep '^tara-sitl-' ; echo "(nothing above = no SITL running)"
tools/diwali_cpu_check.sh          # exit 0 = idle; exit 1 = busy, lists the busiest processes
tools/diwali_cpu_check.sh --wait   # block until idle
```

Idle means no `tara-sitl-*` container and a 1-minute load average under 4 (override with `LOAD_MAX`). The T2
script has two separate gates. Another running `tara-sitl-*` container is a hard refusal that nothing
overrides. A busy CPU is also a refusal, except that `FORCE_BUSY=1` lets a rehearsal go ahead; it then prints
"NOT valid evidence" and the run must not be counted (2.5). On 2 Nov this laptop sat at a load of 16 to 28 for
hours from unrelated jobs (other containers, `pytest`, an `ollama` server, a CPU-burner test), so the T2 runs
had to wait for it (2.5). Expect to have to ask for the machine.

**The SITL slot (team rule, from #172).** Only one person flies Gazebo on this machine at a time, and the
claim is made in writing: before any T2 run, read the latest slot comment on #172. If it says "SITL slot taken",
wait for "SITL slot free". Post "SITL slot taken (who, why)" before you start and "SITL slot free" when the
container is gone. The script's `docker ps` refusal is a backstop, not the claim. The author's T2 runs of 3 Nov
did not post these comments (the latest word on #172 was "free" and no `tara-sitl-*` container was running at
any start), and a later claim on the slot by someone else ended the author's SITL use that night.

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

- **Secrets on a shared machine.** Anything on a command line shows up in the process list for every user.
  Give the T1 script its passphrase through the `TARA_PASSPHRASE` environment variable, never `--passphrase`.
  `tools/diwali_t2_sitl.sh` passes the demo operator's generated passphrase as an argument to a short
  `python -c` once, when it first creates the operators file, so on a multi-user machine that one moment is
  visible in `ps`. It also mounts the whole repository read-write into the container (`-v "$ROOT":/work`), so
  anything running in the container can change your checkout. Neither matters on a single-user laptop; both
  matter on a shared one.
- The Studio backend signs every export with `STUDIO_SIGNING_KEY`; the GCS accepts it because that key is in
  its `trusted_keys`. A key not in `trusted_keys` is refused with 403.
- **Clean install, verified in a fresh clone on 4 Nov** (`origin/main` at `d7e7b2e`, evidence in
  `docs/diwali-runbook-verification/`): `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` took
  163 s and `npm install` in `packages/tara-web` and `packages/tara-studio-web` took 4 s and 5 s, and `./tara doctor`
  then passed every line except the PX4 build and `gz`. **NOT VERIFIED on a cold cache or a machine without
  Python 3.12 and Node 22:** the pip and npm caches were warm and both were already installed.
  The tree that produced the numbers below already had both. `./tara doctor` checks Node (22.23.3 here) and the `node_modules` folders.

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
were clicked on 4 Nov, at 3 drones on the sim backend** (`docs/diwali-runbook-verification/`, screenshots
`dashboard-0` to `dashboard-10`): sign in, select the CSV and its `.sig.json` together, Upload, PRE-ARM CHECK &
TAKEOFF, LAUNCH FLEET, START SHOW, then `STAGED > ... > RUNNING > SHOW_COMPLETE`, then EMER LAND with its
confirm click, then `DISARMED`. Three things differ from the script's path: the sim backend's checklist shows
"Operator confirmation latch: N/A under SIMULATION", so there is **no latch step in the modal** (the script
still calls the latch endpoints, and they return 200 on the sim backend); **the only landing button is EMER LAND**
(two clicks); and **the dashboard stages the mission onto the fleet by itself** 0.5 s after the trajectory upload
(the second Upload button only re-sends it; do not click it for a normal take).

**`diwali_t1_run.py` itself was run on 4 Nov at 3 drones** (`origin/main` `d7e7b2e`, sim backend, load 12):
exit 0, `FORMATION_HOLD` 4 s after launch, show 86.3 s wall for 84.2 s planned, no aborts, landed and
disarmed. Run again against the same stack it refused with "fleet is DISARMED, not STAGED: restart the stack
between runs"; a wrong passphrase and a missing `.sig.json` were each refused with a clear message and exit 1.
**NOT VERIFIED:** the same click path or runner at 35 drones since `7a6ab70`, on a PX4 backend, or on a real GPU.
Rehearse the click path once before recording.

**Why CSV:** the GCS refuses an upload over 5 MB and a CSV over 100,000 rows, and accepts only `.json`, `.csv`
and `.skyc` (not `.tara`). The 35-drone JSON is 9.4 MB (refused), the CSV 1.96 MB (accepted).
**100 drones cannot go through the GCS in any format:** JSON 45.6 MB, CSV 9.9 MB and 352,600 rows. Tested:
`POST /api/upload-trajectory` returned `400 File too large. Maximum size is 5MB.` Raising the limit is a product
change and needs Harsh. Until then, 100 drones is flown like this:

```bash
python tools/diwali_sim_scale.py $D/dist/diwali_100.json --out $D/sim_100.json     # about 8 min, 80 s of it launch stagger
```

**Real show lengths, not the typed ones.** From `examples/diwali/README.md`: each build was certified through the
real `/api/export` (flight export, 0 violations). They are 2.6 to 3.5 times the typed scene times, because the
velocity, acceleration and jerk limits stretch every leg. Plan the slot from the real length, then add the launch
(38 s at 35 drones, T1) and the landing.

**Two lengths for the same storyboard.** The repo example build (`tools/diwali_build.mjs`, the file in
`examples/diwali/results/`) solves the drone-to-point assignment chain in the browser code (`computeAssignmentChainLocal`).
**Generate Sequence in Studio asks the backend (`POST /api/assign-chain`) instead.** The two return different chains, and the
**Return leg** comes out a different length; every other leg is identical. So quote the column that matches the step:
**"Built in Studio" for anything an operator builds on the day, "Repo example build" only for the certified files.**
The certificate (`certify_*.json`) covers the repo example build, not the Studio-built one. At 35 drones Studio's own
validation passed live on the Studio-built show (2.05 m minimum separation, 34.11 m peak, jerk 17.32 m/s³, same as the
certified build). **Why the Return leg differs is NOT VERIFIED** (most likely equal-cost assignments broken differently by
the two solvers; not root-caused). Measured 9 Nov on `f556537`, same builder, standard pacing, 10 Hz.

| Show | Drones | Typed (nominal) | Repo example build | Built in Studio | Min separation | `.tara` size |
|---|---:|---:|---:|---:|---:|---:|
| 3-drone segment (T2 and the T1 clip) † | 3 | 64 s | 84.2 s | not measured | 2.20 m | 40 KB |
| Lettered: diya, rangoli, ring, sphere burst † | 35 | 74 s | 209.5 s | 209.8 s (old pacing) | 2.05 m | 1.2 MB |
| Lettered, with DIWALI (not legible at 35) † | 35 | n/a | 230.8 s | not measured | 2.05 m | 1.3 MB |
| Lettered: HAPPY, DIWALI, diya, rangoli, sphere burst | 100 | 94 s | **416.9 s** | **420.7 s** ‡ | 2.05 m | 6.7 MB |
| **Figures-only fallback** (#237): diya, sphere burst, diya | 35 | 76 s | **207.6 s** | **217.9 s** (3 min 37.9 s) | 2.05 m | 1.2 MB |
| **Figures-only fallback**: diya, rangoli, sphere burst, diya | 100 | 82 s | **324.8 s** | **316.6 s** ‡ | 2.05 m | 5.2 MB |
| Ring and sphere burst (sim scale only) † | 500 | 42 s | 216.4 s | not measured | 2.05 m | 17 MB |

† Pacing before #244 (PX4-safe caps), not rebuilt; the length would be longer now. The 209.8 s Studio figure is from
the old-pacing check in 2.4.
‡ Computed with Studio's own build code (`computeAssignmentChain` against the real backend, then `buildSequencedShow`,
standard pacing), **not read off the Studio screen**; the 217.9 s at 35 drones was read off the Studio screen and the same
code reproduced it exactly. **The Studio-built 100-drone shows were not validated or exported here: NOT VERIFIED.**
The direction is not consistent: at 100 drones the figures-only show is 8.2 s *shorter* in Studio, the lettered one 3.8 s
*longer*. Min separation and `.tara` size in the table are those of the repo example build.

The 35-drone figures-only show uses the stock `diya_flame.svg`; the 100-drone one uses `diya_festive.svg` (the
stock diya is refused at 90 drones or more). The 100-drone shows still cannot go through the GCS (limits above).

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
open the project, **press GENERATE SEQUENCE first**, wait for "Built" and "Safety passed · exports enabled"
in the header, click **FULL-SCREEN PREVIEW**, press Play. `Esc` leaves full screen.

The header's safety badge (beside the Built badge) says what the **Transmit** tab's export buttons will let you
do. Both read one rule, `exportGate()` (PR #226), so the badge cannot say more than the buttons allow. The
buttons it governs are JSON, CSV, Compile .tara, Export Google Earth KML and Transmit to GCS Fleet. The
sequence a walkthrough should show, and what to do at each state:

| Header badge | What it means | Transmit buttons | What to do |
|---|---|---|---|
| (no safety badge) | No show is built yet | none to gate | Press Generate Sequence |
| Validating… | The safety sweep is running (seconds at large fleet sizes) | locked | Wait. It replaces any earlier result while it runs |
| **Safety passed · exports enabled** | Validation PASSED, feasibility not refused, no jerk violations | enabled | Continue. The tooltip says the server re-checks on export and can still refuse the show, so this is "the buttons are open", not "the show is flight-approved" |
| Not flight-certified · exports locked | Passed its hard limits but jerk or feasibility refused it | locked (if jerk is the only failure the tab offers unsigned Preview JSON / CSV / .tara, never signed and never accepted by the GCS) | Do not present it as a certified show. Re-pace and rebuild |
| Safety failed · exports locked | Validation FAILED | locked | Fix the violations in the Safety tab, then Generate Sequence again |
| Unverified · exports locked | A show is built but there is no conclusive result yet | locked | Wait for validation, or Generate Sequence again |

The wording is from PR #226, which checked the first four states live in Chromium at 1440x900 (badge and the JSON
button agreed each time). "PASSED with no show built" is unit- and parity-tested only: a saved project rebuilds on
load, so it cannot be reached in the running app. The measurements below were taken on `7a6ab70`, before #226, so
that run saw the old wording ("export open"). **Re-driven on 4 Nov at 3 drones** (real Chromium, 1440x900,
`docs/diwali-runbook-verification/`, screenshots `studio-1` to `studio-9`): all six states above appeared with
the wording in the table and the Transmit buttons agreed each time. Two of them were stubbed, not real: Not
flight-certified and Safety failed used the real validation response with `feasibility_certified` or `status`
changed (the same method as #226), because every edit I tried to the 3-drone show still certified; Validating
used the real backend with the validation request held back by the test, because it answers in about 100 ms at
3 drones. "No safety badge" was reached for real with Project Vault, New Show. Generate Sequence, FULL-SCREEN
PREVIEW, Play (4.15 s of the 84.2 s timeline played in about 4 s) and `Esc` also worked, with no console errors.
**NOT VERIFIED:** the same at 35 and 100 drones since `7a6ab70`.

- At 35 drones a freshly opened project showed a red **Refused** badge and a 0.00 s timeline until you press
  Generate Sequence (the tooltip read "cannot be made collision-free: two drones would have to swap their
  vertical order in the same column"). **At 3 drones on 4 Nov it did not:** the saved project opened with a
  "Rebuild needed" badge, "Unverified · exports locked" and its stored timeline. Either way, press Generate
  Sequence before the camera rolls.
- The preview is **rebuilt by the Studio UI**, so its length differs a little from the certified file: 209.8 s
  vs 209.5 s at 35, 354.6 s vs 352.5 s at 100 (old pacing). It is the same storyboard, not the certified file. The cause
  is that Studio takes its assignment chain from the backend while the example build solves it locally, which changes
  the Return leg only (see the two-column length table in 2.3; on current pacing: 217.9 s vs 207.6 s at 35).
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
# 5 and 8 drones: build them from the same storyboard against a Studio backend (here a throwaway one on :8012),
# into a folder INSIDE the repo (dist/ is git-ignored): the container sees only the repo, mounted at /work
PYTHONPATH=packages/tara-core:packages/tara-gcs:packages/tara-sim:packages/tara-studio:apps:apps/tara STUDIO_SIGNING_KEY=$D/studio.pem \
  .venv/bin/python -m uvicorn tara_studio.server:app --port 8012 --log-level warning &     # stop it afterwards
node tools/diwali_build.mjs --storyboard examples/diwali/storyboard_3.json --n 5 --tag n5 --api http://127.0.0.1:8012 --out dist/diwali58
node tools/diwali_build.mjs --storyboard examples/diwali/storyboard_3.json --n 8 --tag n8 --api http://127.0.0.1:8012 --out dist/diwali58
VENV=$PWD/.venv tools/diwali_t2_sitl.sh 5 myrun5 dist/diwali58/diwali_n5.json
```

The script starts a fresh container each run (so a missing mission directory cannot hide), copies Node in (the
image has none), starts `./tara up --backend px4 -n N`, waits for N drones, then runs
`tools/sitl_planned_vs_actual.py` (PR #213), which certifies, signs, uploads, launches, starts the show
(`execution="streamed"`, since stock PX4 has no onboard executor), samples Gazebo's own poses and writes a
verdict. Results: `~/tara-sitl-demo-scratch/runs/<label>.json` and CSVs. The container is left running afterwards; stop it with
`docker rm -f tara-sitl-demo` (the script also removes its own previous container at the start of the next run).

**What the author verified of this script**

- Refuses while any other `tara-sitl-*` container runs, and `FORCE_BUSY=1` does not override that (tested
  against another session's container); refuses on a busy CPU unless `FORCE_BUSY=1`. It also refuses to remove a
  container it did not create: `CONTAINER=<someone else's name>` is refused (exit 3) and the container is left
  alone, tested on 3 Nov against a label-less container. It removes only its own, labelled
  `tara.diwali-t2=1`, including its own leftover from the previous run (without that, a second run refused itself,
  because the idle check counts the old container as a foreign SITL; found and fixed on 3 Nov, tested with a
  labelled dummy container, not yet seen across two real runs).
- **Runs of 3 Nov on `main` `b435358` (after #220, #219, #223, #224, #221), through this script, idle gate passed.**
  The 1-minute load average at each start was 3.19 / 3.30 / 3.21 for the 3-drone runs, 3.85 / 3.79 / 3.65 for the
  5-drone runs and 3.76 for the 8-drone run (all under the gate's 4). **The 5-minute average at those moments
  was 6.6 to 12.5 and the 15-minute average 8.4 to 16**, because another session's jobs had only just stopped,
  and the 1-minute load then rose to 13 to 16 during each flight, which is the SITL stack itself. The run records carry `commit: b435358`; the script then lacked the container-removal fix
  above. Load was sampled during the flights only for the first 3-drone run and the 5- and 8-drone runs.
- **3 drones, three consecutive runs, all PASS** and all clean. **5 drones, three runs, all printed PASS, but one
  of them was not a flight of five drones** (below). **8 drones, one run**, PASS and clean.
- **Still NOT VERIFIED:** more than one 8-drone run; 5 and 8 drones with the #222 baseline files (the 5-drone
  build is byte-identical to the 2 Nov one, checked with `cmp`; there is no earlier 8-drone file to compare);
  anything over 8 drones; any run on a host that was not freshly idle.
- The earlier forced attempts on a loaded host (load ~19 to 27) stopped at the launch gate ("drone 2 holds a
  mission but is disconnected"), so a busy host can break the run before it starts.

**Expected numbers (T2): `main` at `b435358`, 3 Nov, clean runs only**

| Drones | Clean runs | Studio min sep | Gazebo min sep | Real-time factor | Start transient | Max tracking error (all drones, whole show) | Worst landing error | Launch to `FORMATION_HOLD` |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 3 | 3 of 3 | 2.20 m | 2.155 to 2.176 m | 0.997 to 0.998 | 0.01 to 0.02 s | 0.29 to 0.32 m | 0.029 to 0.106 m | 10.2 to 10.7 s |
| 5 | 2 of 3 | 2.20 m | 2.106 to 2.126 m | 0.976 to 0.984 | 0.01 s | 0.82 to 0.83 m | 0.038 to 0.053 m | 12.6 to 15.2 s |
| 8 | 1 of 1 | 2.20 m | 2.114 m | 0.984 | 0.01 s | 0.56 m | 0.041 m | 22.8 s |

- **The start dive is gone.** Before #220 (PR #222, flown on `7a6ab70`) the start transient was 3.5 to 7.7 s and
  the max error 5.9 to 15.9 m, from a descent in the first seconds. Now the start transient is 0.01 to 0.02 s
  and the max error stays under 1 m. **Do not tell the audience about a descent any more; do not quote the
  old numbers.** The 2 Nov numbers (RTF 0.993 to 1.000 at 3, 0.980 at 5, **0.913 at 8**) are in PR #222 only.
- **8 drones ran at 0.984, not 0.913.** One run each time, on different code, so I cannot say what changed.
  It is better, not proven better. Prefer 3 or 5 for anything that must look smooth, and do not promise 0.98 at 8.
- **The 5-drone run that did not fly five drones (`rb2-n5-r1`).** The tool printed `PASS`: certified, signed,
  streamed start, `SHOW_COMPLETE`, no abort, Gazebo separation 2.132 m, all disarmed, RTF 0.928. But drone 3
  stayed at altitude 0.00 m on the pad for the whole show (its tracking error: rms 7.8 m, max 10.5 m, against
  0.28 to 0.50 m for the other four). The GCS's own telemetry shows drone 3 in `HOLD` at z about 0 throughout,
  so it is not a Gazebo pose artefact. The GCS log says drone 3 armed, entered OFFBOARD and "climbing to 8.0 m",
  and then the fleet went `TAKEOFF -> FORMATION_HOLD` ("formation_altitude_reached") with it still on the pad,
  and the show started. It did not repeat in the next two 5-drone runs (hold 12.6 s and 15.2 s; this one waited
  18.8 s). **Cause NOT known; not investigated beyond the logs.** Two things are wrong, and neither is fixed
  here: the fleet state machine declared formation altitude with a drone on the ground, and the tool's pass
  rule (`verdict()` in `tools/sitl_planned_vs_actual.py`) cannot see a drone that never flew. Reported on #122.
- **Pass rule** (`docs/sitl-planned-vs-actual.md`): certified, signed, started streamed; `SHOW_COMPLETE` with no
  aborted drone and no RTL/ESTOP/LANDING; Gazebo separation at or above 1.5 m; all disarmed; sim live with no
  gap over 2 s. A pass at real-time factor under 0.9 is marked degraded. **Add this by hand until the tool does
  it: every drone's `max_m` in `actual.gazebo.tracking_error` is under 1.5 m** (a proposed threshold; the 3-,
  5- and 8-drone clean runs are 0.28 to 0.83 m, a drone left on the pad reads 8 to 10 m). Only then count it.
  One quick check: `python3 -c "import json,sys; d=json.load(open(sys.argv[1]))['actual']['gazebo']['tracking_error']; print({k:v['max_m'] for k,v in d.items()})" <label>.json`.
- The Gazebo separation (2.1 to 2.18 m) is slightly under Studio's 2.20 m. Quote the Gazebo number, not Studio's.

### 2.6 When the launch does not get every drone up (#240, merged)

**Read from `origin/main` at `117da30`, not run in this pass: NOT VERIFIED here.** Run on PX4 SITL in PR #240 on
4 to 5 Nov: eight 5-drone launches of the Diwali segment, no false positive across 38 healthy drones, and a forced
fault (drone 3 held in `HOLD` at arm) that was named, refused and landed (`docs/sitl-171-offboard-confirm/`).

**What the GCS now checks.** After a drone arms it has 3 s to be in `OFFBOARD` (`OFFBOARD_CONFIRM_TIMEOUT_S`);
if not, it is disarmed on the pad. Every planned drone must then reach its hover altitude within 15 s plus 1 s per
metre of altitude (`TAKEOFF_BUDGET_*`) while armed and in `OFFBOARD`. A drone that disarms, leaves `OFFBOARD`,
is not connected at launch, or is too slow is recorded in `launch_failures` with the reason. The fleet then
**cannot reach `FORMATION_HOLD`**, so it cannot reach `RUNNING`: this is the abort-all policy. Harsh was asked on
#240 whether the fleet should instead continue without the drone; **that is not implemented.** A failed drone is
disarmed if it is confirmed on the ground, landed if it is in the air and still in `OFFBOARD`, and left alone if
it is out of `OFFBOARD` (a pilot or a PX4 failsafe has it).

**What the operator sees when `launch_failures` is not empty.**

| Where | What shows | State |
|---|---|---|
| Dashboard header | `FLEET: TAKEOFF` and it does not move on. START SHOW is greyed (the dashboard only enables it in `FORMATION_HOLD`) | read from the code, NOT VERIFIED on screen |
| Dashboard, which drone and why | **LaunchFailureBanner + DroneCard refusal strip:** Shows prominent red banner `LAUNCH INCOMPLETE — SHOW START REFUSED (N DRONES)` listing each failed drone and reason, marks failed cards with `⚠ LAUNCH REFUSED: <reason>`, highlights failed drones under the Attention filter chip, and displays refusal warning under START SHOW | Fixed and verified on main |
| `GET /api/fleet-state` and `GET /api/status` | `"launch_failures": {"3": "<why>"}`, for example "left OFFBOARD for HOLD ..." or "reached only 0.20 m of its 8 m hover altitude within 23s" | SITL-verified in #240 |
| `POST /api/start-trajectory` | `409`, `error: launch_incomplete`, message "Show start refused: ... Drone 3: <why>. Land the fleet and relaunch." and a `start_trajectory_refused` audit record. **Only reachable through the API**, because the button is greyed | SITL-verified in #240 |
| GCS terminal | `Drone N launch failed: <why>. The show cannot start without it.` | read from the code |

**What to do.** (1) Do not wait for it to fix itself: the fleet stays in `TAKEOFF` until it is landed. (2) Find the
drone and the reason (API call below, or the GCS terminal). (3) EMER LAND, then CONFIRM LAND?, and wait for
`DISARMED`. (4) Fix or exclude the cause (a drone that is not connected, a stuck pad, a drone held in `HOLD`), then
relaunch; `LAUNCH` is legal from `DISARMED` in `fleet_fsm.py`, but a relaunch through the dashboard was **not run
here**, so restarting the stack is the safe path (it takes about 20 s). (5) Discard the take.

```bash
# On the GCS machine (single-user laptop: the passphrase shows in `ps` for a moment, see 2.2). NOT RUN in this pass.
TOKEN=$(curl -s -X POST localhost:8090/api/auth/login -H 'Content-Type: application/json' \
  -d "{\"operator\":\"demo\",\"passphrase\":\"$PASS\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -s localhost:8090/api/fleet-state -H "Authorization: Bearer $TOKEN" | python3 -m json.tool | grep -A6 launch_failures
```

### 2.7 PX4-safe pacing (PR #244, merged)

**Merged into `main` on 6 Nov (#244).** Fast pacing is hidden from the Studio menu and capped at PX4-safe budgets.
The SITL evidence in the PR confirmed that old Fast was not safe: on 10 drones on PX4 SITL + Gazebo, Fast lagged the plan by up to 15.9 m with a
closest approach of 0.79 m and a proximity failsafe RTL, and the capped Fast variant that passed kept only 1.70 m
against the 1.5 m abort limit. **Use Standard. Do not select Fast, and do not rebuild a show with it, on the day.**

**What #244 changes (from its description, not run in this pass):** climb budget 2.4 m/s and descent 1.2 m/s
(PX4 defaults `MPC_Z_VEL_MAX_UP` 3.0 and `MPC_Z_VEL_MAX_DN` 1.5), acceleration inside `MPC_ACC_HOR` 3.0, for both
profiles; Fast stays in code but is hidden. The old Standard also asked for 2.4 m/s descents at fleet scale (10 % of
the 200-drone show was over PX4's 1.5 m/s descent cap). Standard shows get longer: the 200-drone show went from
546 s to 721 s and the Studio demo show from 118.9 s to 124.4 s.

**What that means for this runbook once #244 merges, none of it done:**

- Every show length, `.tara` size and T1 number in 2.3 is from the old pacing. Rebuild and re-certify the shows
  that will be flown (`tools/diwali_run_all.sh`, 2.3) and replace the lengths, **especially the 100-drone shows**,
  which were built with the old Standard and may contain descents above 1.5 m/s. Whether the 35- and 100-drone
  Diwali shows change at all is **NOT VERIFIED**: nobody has rebuilt them with the new budgets.
- The T2 numbers in 2.5 are for the old pacing and the 3-drone segment; re-run them on the commit that will be
  recorded.
- Only 10 drones were ever flown on PX4 with the new budgets. The 35- and 100-drone shows have never been flown on
  PX4 at all (T2 stops at 8 drones in the container, 10 on the native build).
- If #244 is not merged before the Sat 7 Nov freeze (section 6), the demo runs the old pacing: say so, and do
  not build any new show in Studio on the day.

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
- [ ] Studio project open, **Generate Sequence pressed**, badge reads Built and Safety passed · exports enabled (2.4).
- [ ] GCS signed in; header shows `MODE: SIMULATION · tara_sim (kinematic)` (T1) or the px4-sitl label (T2).
- [ ] The deviation panel is green (see 2.3). No red banner in frame.
- [ ] Window exactly **1440x900**.
- [ ] Know the T1 dashboard click path (4 Nov, 3 drones): sign in; click the upload box and select the show CSV and
  its `.sig.json` together (the mission stages itself, 0.5 s later); PRE-ARM CHECK & TAKEOFF, LAUNCH FLEET (no
  latch step on the sim backend); wait for `FLEET: FORMATION_HOLD`; START SHOW; after `SHOW_COMPLETE` the landing
  control is EMER LAND then CONFIRM LAND? (there is no plain Land). The Fleet
  Telemetry cards used to print unrounded numbers that overflowed their boxes; `52d5e54` (merged) formats them,
  and the dashboard was reworked by #93, so **this click path and the card layout are NOT VERIFIED on the current
  `main`**: rehearse it once on `117da30` or later.

### 4.2 What must be visible, and the limits of this rig

The ask is Gazebo, RViz and the GCS dashboard together.

| Pane | Who and how | State |
|---|---|---|
| GCS dashboard (ops, Earth, Night Sky) | Dhiraj, `http://localhost:5173` | Verified in a browser; a clean 1440x900 capture works (below) |
| Studio full-screen preview | Vishal (Studio owner), `http://localhost:5174` | Verified (2.4) |
| **Gazebo** | The container is **headless**, and Docker Desktop's VM has no display path to the host (`docs/sitl-planned-vs-actual.md`). No Gazebo window exists on the container rig. | **Made on 5 Nov on a native, user-space build on this laptop** (Gazebo Sim 8.10.0, PX4 v1.16.2 built with three local patches, `./tara up --backend px4 -n N --gui`): clips at 1, 3 and 10 drones (4.4, not yet in the repo). **Not the stock build** and not re-run in this pass. On the container rig a Gazebo window is **NOT VERIFIED** (it needs a native install). |
| **RViz** | **Harsh** (owns the #171 recording). It is not in the container: the image has no ROS 2. `sim/ros/launch_rviz.sh` needs ROS 2 Jazzy, `ros_gz_bridge`, `rviz2`, a display, and the same Gazebo transport partition as the fleet. | **Made on 5 Nov** in the clips (4.4): ROS 2 Jazzy `rviz2` 14.1.24 from RoboStack in a user-space environment, run from a local copy of the repo's RViz config, not through `--rviz`. Not re-run in this pass. **NOT VERIFIED** across the Docker Desktop VM boundary (no ROS 2 in the image). As of 3 Nov the script's header still named `scripts/launch_fleet.sh`, which no longer exists; not re-checked. |

The three-pane Gazebo + RViz + dashboard recording **was made on 5 Nov** (4.4), on a local build, with RViz run
from a local copy of the repo config. Whether the day uses those clips, or a live window on a machine Harsh
names, is his decision and is not recorded. Do not splice a Gazebo screenshot from elsewhere into a clip and call
it this run.

Browser-only capture at exactly 1440x900 works: Playwright `recordVideo` with a 1440x900 viewport produced a
valid `.webm` (checked: file type WebM). The laptop's own display is 1920x1080 with no 1440x900 mode in
`xrandr`, so a whole-screen recording is the wrong size; use a 1440x900 window or region. GNOME's built-in
screen recorder is **NOT VERIFIED**. The 5 Nov clips used `ffmpeg` n9.0.2 from a Python wheel (`imageio-ffmpeg`
in a venv, no system install) with `x11grab` on display `:1`, a 1440x900 region, 30 fps for T1 and H.264 crf 23
for T2; the notes next to each clip record the exact region.

### 4.3 Naming and storage

`diwali2026_<tier>_<N>drones_<what>_<YYYYMMDD>_<sha7>.<ext>`, for example
`diwali2026_T1_35drones_gcs-dashboard_20261105_b435358.webm` and
`diwali2026_plan_100drones_studio-fullscreen_20261105_b435358.webm`. Tier is one of `plan`, `T1`, `T2`.
There is no `T3`. Next to every clip keep its `<label>.json` run record (T2) or the terminal log (T1).

- **Not in git.** Clips are large, and the evidence pack says it must not contain a simulated recording
  (`docs/evidence-pack.md`, section 8).
- Working copy: `~/diwali-recordings/` on the recording machine. **The shared location does not exist yet and is
  Harsh's decision.** Nothing in the repository or the evidence pack names one.
- No music has been chosen and no licence recorded (#132). A silent clip is the default.

### 4.4 Clips made on 5 Nov, not yet in the repository

Made on the laptop of the author, in the worktree `tara-wt-t1rec`, in `docs/recordings/`, where git lists them as
untracked. **None is committed or pushed.** Section 4.3 says clips stay out of git and the evidence pack says it
must not hold a simulated recording; nothing changes until Harsh decides where they are published (4.3). Each has
a `.notes.json` beside it with its tier, commit, capture region, what it is **not**, and the deviations. All were
recorded on `d7e7b2e`. None was re-run or re-watched in the 6 Nov pass.

| Clip (`diwali2026_...`) | Length, size | Show | Read before showing it |
|---|---|---|---|
| `T1_3drones_studio-preview-gcs-dashboard_20261105_d7e7b2e` | 2:57, 12.6 MB | the certified 84.2 s 3-drone segment, signed, on the sim backend | T1, `tara_sim`: **no Gazebo, no PX4, no RViz**, no landing, no audio |
| `T2_1drone_px4-sitl-gazebo-rviz-gcs-dashboard_20261105_d7e7b2e-localbuild` | 2:05, 4.2 MB | a 1-drone build of `storyboard_3.json` | T2 on a **local build** (Gazebo 8.10.0, patched PX4); not the #171 hover sequence (API-driven, not the dashboard button); no landing |
| `T2_3drones_px4-sitl-gazebo-rviz-gcs-dashboard_20261105_d7e7b2e-localbuild` | 2:31, 5.1 MB | the 84.2 s segment (the GCS reports 92.3 s with the start leg) | T2 local build; `SHOW_COMPLETE`, no abort; Gazebo 98 to 99 % real time with the GUI, RViz and recorder; ground-truth separation and tracking **not measured** for this clip |
| `T2_10drones_3d-formations_px4-sitl-gazebo-rviz-gcs-dashboard_20261105_d7e7b2e-localbuild` | 3:56, 4.6 MB | three 10-point formations (diya, lotus fan, kandil star), certified through the real `/api/export` (156.6 s, min separation 2.05 m) | T2 local build **and a scratch-only `points` shape** that Studio did not have at the time (the repo is unmodified; the shapes are not reproducible in the UI as of `d7e7b2e`); below 100 % real time (82.91 % in the frame at t=60 s; 30 frames sampled every 8 s read 76.22 to 93.05 %, 26 of them between 85 and 92 %); flown positions within 0.12 m of design; the drones are about 10 px in the Gazebo pane, so RViz carries the legibility |

Two things every clip shows that are not bugs of the recording: after `FORMATION_HOLD` the drones descend to the
pads and take off again into the first formation (the GCS start leg, so two climbs), and a T2 clip is simulation,
not a flight. The clip filenames carry `localbuild` for that reason. Not covered by any clip: the container rig,
8 drones, the 35- and 100-drone shows, anything after #240 or #244.

## 5. Who does what, abort and rollback

### 5.1 Who does what

Roles are from the 1 Nov ownership split and the issues; the recording split is **proposed, for Harsh to confirm**.

| | Harsh | Vishal | Dhiraj |
|---|---|---|---|
| Decisions | Storyboard sign-off; whether #135 is attempted; firmware; approves any fix to the upload limit, the snapshot, or the 5-drone no-takeoff finding (2.5); the shared recording location; music rights | Studio calls | none |
| Review | Super-review before every merge; frozen from Sat 7 Nov | Reviews the follow-up PRs to this runbook; Studio PRs | Does not merge |
| Studio side | | Full-screen preview and operator UX (#132); Studio walkthrough recording (proposed) | Shows are built and certified |
| GCS, sim, Gazebo | | | T1 and T2 runs, run records, evidence-pack rows |
| Hardware / RViz / Gazebo window | #135, RViz, the machine with a display (proposed); **names the RC pilot (5.3)** | | Supplies the commands in this document |

### 5.2 Abort and rollback

Fleet commands (`/api/rtl`, `/api/land`, `/api/estop`, and per drone `/api/drone/<id>/rtl|land|estop`) are the
same as the dashboard's EMERGENCY RTL, EMER LAND and E-STOP (KILL). E-STOP kills motors; airborne drones fall.
**E-STOP, verified on 4 Nov on the sim backend only** (3 drones, mid-show, twice; screenshots
`dashboard-11` to `dashboard-13` in `docs/diwali-runbook-verification/`): the first click on E-STOP (KILL) only
arms a "CONFIRM KILL?" button and sends nothing; the second sends `POST /api/estop` (200), the fleet goes
`RUNNING > ESTOP > DISARMED`, armed drones fall from 3 to 0, and each drone card reads TERMINATED. The fleet state
then reads `DISARMED`, not a latched `ESTOP`. **NOT VERIFIED:** that motors stop and a drone falls (the sim has
no motors), E-STOP on PX4 SITL, and E-STOP on hardware.

| Event | What it looks like (verified where stated) | What to do |
|---|---|---|
| **One drone aborts** | Verified on T1, 35 drones, 25 s into the show: `POST /api/drone/7/rtl` returned 200; the GCS modelled drone 7's RTL path passing **0.73 m from drone 9** and 2.00 m from drone 11, and ~3 s later the proximity monitor fired `failsafe:proximity` and **RTL for the whole fleet**. The show ended and the fleet landed. | An abort ends the take for everyone. **Discard the take, restart the stack, rerun.** Do not demonstrate a single-drone abort on camera in a formation. One observation, one time offset; not characterised further. T2 not tested by the author. |
| **RTL fires** | `/api/fleet-state` history names the trigger: `failsafe:proximity`, `failsafe:geofence`, or `POST /api/rtl`. Verified: after `POST /api/rtl` mid-show the fleet reached `DISARMED` within 15 s. | Read the trigger, keep the JSON, **discard the take**. Restart the stack: a finished run leaves the state `DISARMED` and the runner needs `STAGED`. Whether a restart can be avoided is NOT VERIFIED. |
| **Sim stalls** | T2: the tool marks the run DISCARD if Gazebo poses stop for more than 2 s, and marks it degraded under 0.9 real time. T1: the show took more than ~15 s longer than planned, or the deviation panel went red (**15 s is a proposed threshold, not a measured one**). | **Rerun. Do not count it.** First ask whether the machine was idle: `tools/diwali_cpu_check.sh`. |
| **A planned drone does not get up at launch** | The fleet stays in `TAKEOFF`; the header never says `FORMATION_HOLD`; START SHOW is greyed. Details and commands in 2.6. | Read which drone and why (2.6), then EMER LAND and relaunch. Do not look for a way to start without it. |
| **Another session kills your stack** | The `tara up` terminal prints `received SIGINT, shutting down...` with nobody having pressed Ctrl+C; the dashboard loses its GCS mid-show. Seen 5 Nov, twice in three 35-drone attempts, a few seconds after another session's launcher started a native fleet. The cause was not proven. | Discard the take. Check `pgrep -cx px4` and `ss -ltnp` for what else is running, and do not restart until the machine is quiet (2.1). |
| **Docker dies** | Verified after a reboot: `docker ps` says it cannot connect to the daemon. `systemctl --user start docker-desktop` restored it in ~25 s; images stayed, containers were gone. | Start Docker Desktop, then rerun in a **new** container (the script does that). A T2 run that lost its container is discarded. T1 does not use Docker and is unaffected. |
| **Stuck stack** | `Ctrl+C` in the `./tara up` terminal stops the stack (verified with SIGINT). | `./tara reset` runs `pkill -9` on `px4`, `mavsdk_server`, `gz sim`, `uvicorn tara_gcs`/`tara_studio` and `vite` on ports 5173/5174. **It kills any native stack of the same project on the machine, whoever started it. Verified on 4 Nov (sim stack, 3 drones, supervisor killed with `kill -9`): it removed all six orphans (2 uvicorn, 2 vite wrappers, 2 vite), freed ports 5173, 5174, 8001 and 8090, and `./tara status` then said "nothing running".** Before running it, `./tara status` lists what it would kill. It cannot reach processes inside Docker Desktop's VM (with a SITL container up, the same patterns matched nothing on the host), so it neither stops nor affects a T2 container; use `docker rm -f` for that. It was not run against another session's native stack, by design. Use it only when nothing else of yours is running. `sim/px4/stop_fleet.sh` also kills every `mavsdk_server`, including a running GCS's. |

### 5.3 Real-drone abort and RC takeover (only if Harsh signs #135; otherwise skip this section)

**NOT FLIGHT READY.** This build is not cleared to fly hardware, and this section does not clear it. It is the
procedure that must exist, with a name filled in, *before* a real hover is attempted on Sun 8 Nov. It was
written from #135 (sections B and D), the GCS code and the simulator runs. **No step in it has been run on a
real vehicle.** The airframe, flight controller and firmware are not recorded anywhere (#135 section A is
still open) and the firmware is undecided, so anything that depends on them is left as a blank to fill, not
guessed.

**People.** Nobody is named for the pilot role in #135, in any other issue, or in this repository.

| Role | Name | Duty |
|---|---|---|
| **RC pilot** | **NOT YET NAMED. Harsh (owner of #135) writes the name here.** | Holds the transmitter, thumbs on the sticks, from arming to disarm. The only person who can abort when the telemetry link is down. Calls ABORT. |
| Flight sign-off | Harsh | Signs #135 before any propeller is fitted; decides go / no-go on the day. |
| GCS operator | Dhiraj (proposed) | Watches `/api/mode` and telemetry, presses the GCS abort the pilot calls. Never touches the sticks. |
| Safety observer | NOT YET NAMED | Keeps people out of the test area. |

**Hard rule: no name in the RC pilot row, no flight.** The named pilot must have read this section.

**Before propellers are fitted** (all of #135 section B, propellers removed; any ambiguity stops the day):

- [ ] RC link works. The pilot flips the takeover switch and the new mode shows on the GCS. This is the only
      confirmation of takeover there is, because the GCS cannot verify it (`rc_takeover_available` is hard-coded false).
- [ ] The kill / disarm switch is mapped, and the pilot has used it on the bench.
- [ ] Loss-of-RC and loss-of-link failsafes are configured and their behaviour is written down here: `__________`
- [ ] The autopilot's return-to-launch altitude is read off the vehicle and written here: `______ m`
- [ ] Geofence and battery failsafes are configured. Telemetry shows `FRESH` (under 2.5 s old).
- [ ] The software commit is noted and the run is rehearsed in T2 first (the hover path, `POST /api/single-drone/hover-sequence`).

**Abort criteria** (from #135 section D; anyone present may call it, nobody argues): oscillation, unexpected
drift, stale telemetry, a rejected command, an unexpected mode change, or RC takeover or failsafe in doubt.

**Abort ladder** (try the first that applies; speed matters more than choosing the nicest one):

| # | Action | Who | Status |
|---|---|---|---|
| 1 | **Take over on the RC** (switch to the manual or position-hold mode agreed on the bench) and land by hand. The sequence notices the vehicle has left OFFBOARD and stops commanding it (`OperatorTakeoverError`). | RC pilot | The software yield was seen in PX4 SITL with a *simulated* RC stream (#171, #145). **Never with a physical transmitter or a real vehicle: NOT VERIFIED.** Mode names depend on the firmware, which is undecided. |
| 2 | **GCS LAND** (`POST /api/drone/0/land`, the EMER LAND button): descend in place. | GCS operator | The fleet `POST /api/land` ended every T1 and T2 run here; the per-drone route `/api/drone/0/land` itself was **not called** by the author. **Not run on hardware.** Needs a live link. |
| 3 | **GCS RTL** (`POST /api/drone/0/rtl`). **Do not use at a 1 m hover** until the return altitude is written above: an autopilot RTL may climb to its configured return altitude first. | GCS operator | Fleet `POST /api/rtl` and `/api/drone/7/rtl` were called in T1 only (5.2). T2 and hardware behaviour **NOT VERIFIED**. |
| 4 | **Kill** (the mapped kill switch, or E-STOP (KILL) in the GCS). Motors stop and the vehicle falls from wherever it is. Last resort, only over clear ground. | RC pilot, or GCS operator | **E-STOP: the GCS command path was run on the sim backend on 4 Nov (5.2). NOT VERIFIED on PX4 or hardware, and the mapped RC kill switch was never tested.** |

**If the link drops** (telemetry goes `STALE`, or the GCS stops answering): rungs 2 to 4 may not reach the
vehicle. The hover code raises on a stale heartbeat and tries to recover, but that needs a link too. The
vehicle's own failsafe and the RC pilot are the abort. Keep the transmitter in hand until the motors are off.

**After any abort:** do not re-arm. Keep `/api/fleet-state` history, the GCS and audit logs and the video;
write down what was seen and what was pressed; Harsh decides whether any further flight happens that day.
Label everything *hardware, aborted*, never as a pass.

**Rollback.** The last commit with a full recorded T2 set after #220 is `b435358` (3 Nov): 3 drones three times,
5 drones twice clean (a third run left a drone on the pad), 8 drones once. If `main` changes after that and a
rerun does not match the section 2.5 table, record from a checkout of `b435358`. `7a6ab70` (PR #222) is the older
baseline and **has the start descent**; use it only if something after #220 breaks and say that the descent is
visible. **Nothing merges on Sat 7 Nov**; the 8 Nov recording uses the commit that passed the Saturday dry run,
and that SHA is in every filename. Re-run the section 2.5 set on that commit before the dry run, not just on the day.

## 6. Day by day, 2 to 8 Nov

Only items that exist today. Owners are proposed. Every item that needs a merge needs Harsh's approval first.

| Day | Item | Owner | Depends on |
|---|---|---|---|
| Mon 2 | Done: #216, #219, #220, #222, #225 merged. Still open for Harsh: whether #135 is in play | Harsh | none |
| Mon 2 | Harsh names the machine for the Gazebo window and RViz, or confirms the T2 clip is dashboard plus run record | Harsh | decision |
| Tue 3 | Done early (night of 2 to 3 Nov): 3-drone ×3, 5-drone ×3, 8-drone ×1 on `b435358`, table in 2.5. **Open:** the 5-drone run where drone 3 never took off, 8 drones repeated, and the same set again on the commit that will be recorded | Dhiraj | idle machine; Harsh on the 5-drone finding |
| Tue 3 | Studio preview at 35 and 100: confirm the Generate-first step on a real GPU session | Vishal | none |
| Wed 4 | T1 35 drones through the GCS on an idle machine, three clean runs, logs kept; rehearse the dashboard button path once | Dhiraj | idle machine |
| Wed 4 | 100-drone clip plan: `diwali_sim_scale.py`, start after settling | Dhiraj | none |
| Thu 5 | Recording pass 1: Studio preview 35 and 100, GCS dashboard T1 35, named per 4.3 | Vishal, Dhiraj | 1440x900 window |
| Thu 5 | Recording pass 1: T2 3 drones (Gazebo + RViz + dashboard if Harsh's machine exists, else dashboard + run record) | Harsh, Dhiraj | Mon decision |
| Fri 6 | Retakes of anything that aborted, stalled or went red; evidence-pack rows with the SHA (#134); music decision (#132) or silent | Dhiraj, Harsh | none |
| Sat 7 | **Freeze**: no merges. Full dry run of this document from a stopped machine, top to bottom | Dhiraj | Fri |
| Sun 8 | Demo day. Real hover only if Harsh has signed #135 **and 5.3 has a named RC pilot**; otherwise the demo is simulation and says so | Harsh | #135, 5.3 |

**Status on Fri 6 Nov, from the repository and the PR list (nothing run in this pass):**

- Done: #237 (figures-only shows), #240 (launch refusal), #241 and #243 (SITL acceptance and the 10 repeats), #242
  (the no-SITL runbook steps at 3 drones), #244 (PX4-safe pacing & auto light show), #245 (voltage format),
  the T1 and T2 clips (made, not in the repo, 4.4).
- Not done: three clean T1 35-drone runs on an idle machine (three attempts on 5 Nov, none finished: two stacks
  were killed from outside and one was stopped by a load above 8); the 100-drone `diwali_sim_scale.py` run and the
  35- and 100-drone Studio preview since the pacing change; any run on the commit that will be recorded; the RC
  pilot's name (5.3); Harsh's choice between lettered and figures-only, and between abort-all and
  continue-without-the-drone (2.6).

## 7. Verification log (2 to 6 Nov 2026)

| Item | Result |
|---|---|
| `./tara doctor` | Python, Node 22.23.3, `node_modules` OK; PX4 build and `gz` FAIL on the host (expected) |
| Build and certify 3, 35, 100 | OK; 35 and 100 byte-identical to #212's builds; 5 and 8 certify |
| Signed CSV export, 35 and 100 | OK; 100 is over the GCS limits (warned by the tool, refused by the GCS: 400) |
| T1 35 drones, 3 full runs through the GCS API | 211.9 / 213.2 / 213.8 s, none aborted, all landed |
| T1 100 drones, `diwali_sim_scale.py` | RTF 0.936, settled 14.3 s, flown min sep 2.042 m |
| Studio preview 35 and 100 | OK (Playwright, headless Chromium, 1440x900) |
| GCS dashboard sign-in, Operations, Earth, Night Sky | Rendered; clicks on upload and PRE-ARM buttons not made (2 Nov) |
| 4 Nov, `origin/main` `d7e7b2e`, fresh clone, 3 drones, sim backend, no Docker (`docs/diwali-runbook-verification/`) | Clean `pip`/`npm install` and `./tara doctor` PASS (warm caches); 3-drone build and signed CSV PASS; Studio six header states PASS (Not flight-certified and Safety failed with a stubbed validation response, Validating with a held request); Studio Generate, full-screen preview, Play, `Esc` PASS; dashboard button path sign-in to `DISARMED` PASS (no latch step on the sim backend; only EMER LAND lands; the mission stages itself); `diwali_t1_run.py` at 3 drones PASS, 5 Nov; E-STOP command path PASS (twice); `./tara reset` PASS; `Ctrl+C` PASS |
| Single-drone RTL and fleet RTL mid-show (T1) | Fleet-wide proximity RTL after drone 7's abort; fleet RTL reached DISARMED |
| T2 through `diwali_t2_sitl.sh`, 2 Nov, 3 drones, one run, before #220 | PASS: Gazebo min sep 2.192 m, RTF 0.997, landing under 4 cm, start dive 5.9 m |
| T2 through `diwali_t2_sitl.sh`, 3 Nov, `b435358` | 3 drones ×3 clean; 5 drones ×3 (2 clean, 1 with a drone left on the pad, still `PASS`); 8 drones ×1 clean. Numbers in 2.5. Dive gone. |
| T2 script guards, 3 Nov | Foreign container named by `CONTAINER` refused and untouched; own labelled container removed; the idle check no longer counts it. Tested with dummy containers, not yet across two real runs after the fix |
| Gazebo window and RViz on the stock build or the container, GNOME's recorder, E-STOP motors actually stopping or on PX4/hardware, 35- and 100-drone Studio preview since `7a6ab70` and since #244, the dashboard on current `main` (#93) and on a PX4 backend, the launch-refusal display on screen (2.6), a cold-cache install | **NOT VERIFIED** |
| Real-drone abort and RC takeover (5.3), a named RC pilot, GCS LAND on hardware, any hardware flight | **NOT VERIFIED. NOT FLIGHT READY.** |
| Windows | Does not work (Platform note at the top); not fixed |
| 4 Nov, `diwali_t1_run.py` at 3 drones, sim backend | PASS: exit 0, 86.3 s wall for 84.2 s planned, landed. A second run on the same stack ("fleet is DISARMED, not STAGED"), a wrong passphrase and a missing `.sig.json` were each refused with a clear message |
| 4 to 5 Nov, #240 SITL on PX4 (PR #240, `docs/sitl-171-offboard-confirm/`) | 8 five-drone launches, 38 healthy drones confirmed, 0 false positives; drone 3 held in `HOLD` at arm: named, disarmed on the pad, `409 launch_incomplete`, landed to `DISARMED` |
| 4 to 5 Nov, #243 (`docs/`, PR #243) | 10 repeats of the 5-drone segment on `d7e7b2e`: every drone flew; the no-takeoff run of 3 Nov did not recur (not called fixed: a 1-in-7 event would not show in 10 runs about one time in five) |
| 5 Nov, T1 35 drones through the GCS, three attempts | None finished. Builds certified (35 drones 9.39 MB, 100 drones 45.6 MB), signed CSV 1.96 MB and 73,360 rows, launch to `FORMATION_HOLD` 38 s each time. Two stacks received SIGINT from outside a few seconds into the show (load under 4, not sent by my watcher); one was stopped by my own rule (load above 8 for 30 s) when another session started a native fleet. No 35-drone show was flown to the end |
| 5 Nov, clips (4.4) | T1 3 drones, T2 1, 3 and 10 drones made on `d7e7b2e`, local build for T2; not in the repo |
| 6 Nov, this refresh | **Docs only. Nothing was run.** Every row above dated before 6 Nov stands on its own date and commit; nothing was re-verified on `117da30` |

**Overlap disclosure.** Four of the author's runs shared the machine with another session's `tara-sitl-171fix`
(Gazebo): a headless browser for about 70 s from 04:35; the last ~1 minute of the 05:32 T1 run (the container
appeared at about 05:35); a 5-drone kinematic stack for about 1 minute at about 05:38, started after `docker ps`
had printed that container (the check was only printed, not enforced: an error); and a T2 attempt at 05:37 that
was stopped within seconds. None was Gazebo work, but that session's real-time factor in those windows should be
treated as perturbed. The T2 script's SITL gate was made non-overridable after the 05:37 attempt.
