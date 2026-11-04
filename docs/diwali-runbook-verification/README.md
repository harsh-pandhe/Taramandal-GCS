# Runbook verification without SITL (4 Nov 2026, #122)

What this is: the steps of `docs/diwali-runbook.md` that were marked **NOT VERIFIED** and do not need Gazebo or
PX4, run on current `origin/main` (`d7e7b2e`) in a fresh clone, with screenshots. Nothing here touched product
code. No Docker container was started, no test suite was run, and the fleet was 3 drones on the `sim`
(kinematic, T1) backend throughout. Every process of mine ran at `nice -n 19`.

Method: a fresh `git clone` of this repository (its local object store, checked out at `d7e7b2e`) in
`~/tara-verify-clone`, a throwaway key, operator and `tara.toml` (`backend = "sim"`) in `~/tara-verify-demo`, a
headless Chromium (Playwright, 1440x900, software WebGL) driving the real Studio (`:5174`) and dashboard
(`:5173`) against the real backends. The Playwright scripts are not committed (docs and screenshots only);
the method per step is below, and the raw results are in `studio-header-states.json` and
`dashboard-flight-calls.txt`.

## Results

| Step | Result | Evidence |
|---|---|---|
| Clean install in a fresh clone | **PASS, with caveats** | below |
| `./tara doctor` in that clone | PASS for Python, mavsdk, scipy, numpy, npm, Node 22.23.3, both `node_modules`, the world file. FAIL for the PX4 build and `gz` (expected on this host) | below |
| 2.3 build and certify, 3 drones (`tools/diwali_run_all.sh`) | PASS: 3-drone show certified, 0 violations, min separation 2.2 m, byte-identical to the build made through the running stack's Studio (`cmp`) | below |
| 2.3 signed CSV export (`tools/diwali_export_csv.py`) | PASS: 2,529 rows, signed | below |
| 2.4 Studio, six header states | **PASS for 4 live states, 2 with a stubbed validation response** (see the table) | `studio-1` to `studio-6` |
| 2.4 Generate, FULL-SCREEN PREVIEW, Play, `Esc` (3 drones) | PASS | `studio-7`, `studio-8`, `studio-9` |
| Dashboard buttons on the sim backend, 3 drones | **PASS**: sign in, upload show + `.sig.json`, upload mission, PRE-ARM CHECK & TAKEOFF, LAUNCH FLEET, START SHOW, EMER LAND, to `DISARMED` | `dashboard-0` to `dashboard-10` |
| E-STOP (KILL) on the sim backend | **PASS for the command path** (see limits), run twice | `dashboard-11`, `dashboard-12`, `dashboard-13` |
| `./tara reset` | **PASS** on a stack killed with `kill -9` | below |
| `Ctrl+C` (SIGINT) stops the stack | PASS (again) | below |
| T1 35 and 100 drones, 35/100-drone Studio preview, `tools/diwali_t1_run.py` | **NOT RUN** (the brief says 3 drones only) | |
| Anything with Gazebo, PX4, Docker, RViz, recording, Windows, a real GPU | **NOT RUN** | |

### Studio header states (2.4), 3 drones

Each state is on the Transmit tab so the badge and the export buttons share a frame; the JSON, CSV, `.tara`, KML
and Transmit buttons' `disabled` flags are in `studio-header-states.json` and agree with the badge every time.

| # | Header badge | Export buttons | How it was reached | Screenshot |
|---|---|---|---|---|
| 1 | (no safety badge) | all disabled | **real**: Project Vault, New Show, nothing generated | `studio-1-no-badge.png` |
| 2 | Validating… | all disabled | **real backend**, but the test held `/api/validate` back until after the screenshot, because at 3 drones it answers in about 100 ms | `studio-2-validating.png` |
| 3 | Safety passed · exports enabled | all enabled | **real**: Generate Sequence, real validation PASSED and certified | `studio-3-safety-passed.png` |
| 4 | Not flight-certified · exports locked | all disabled | **STUBBED**: the real response with `feasibility_certified` set to `false` (same method as #226) | `studio-4-not-flight-certified.png` |
| 5 | Safety failed · exports locked | all disabled | **STUBBED**: the real response with `status` set to `FAILED` (same method as #226) | `studio-5-safety-failed.png` |
| 6 | Unverified · exports locked (with "Rebuild needed") | all disabled | **real**: the saved project as opened, before Generate Sequence | `studio-6-unverified.png` |

I tried to reach states 4 and 5 for real by editing hold, morph and layer-gap fields in the 3-drone show. Every
edit still certified (the sequencer re-paces), so those two states were not reached without a stub. No console
errors on any Studio page.

## Findings (none fixed)

1. **Dashboard telemetry cards print unrounded floats and overflow their box** (real, cosmetic). On the sim
   backend `components/fleet/DroneCard.jsx` renders `${drone.local_x}m` unformatted, so the card shows
   `0.019999999552965164m0.009999999776482` running out of the card (`dashboard-3-mission-uploaded.png`, in the
   Fleet Telemetry cards, below the first screen). It is below the fold at 1440x900, but a scroll or a taller window
   shows it. Where the long number comes from (the sim payload) I did not trace.
2. **The runbook said the dashboard flow has a two-step latch; on the sim backend it has none.** The checklist
   shows "Operator confirmation latch: N/A, Not checked under SIMULATION" (`dashboard-4-preflight-flight.png`),
   so the modal goes straight to LAUNCH FLEET. `tools/diwali_t1_run.py` still calls the latch endpoints; the
   dashboard on a PX4 backend was not tried (needs SITL).
3. **The only landing button is the emergency one.** After SHOW_COMPLETE the dashboard offers EMER LAND (two
   clicks, "CONFIRM LAND?") and nothing named plain Land (`dashboard-9-land-confirm.png`). It worked
   (`SHOW_COMPLETE > LANDING > DISARMED`). Say so on camera or use the T1 runner for a clean landing.
4. **`POST /api/upload-mission` is sent twice** per upload in the dashboard flow (once automatically after the
   trajectory upload and once from my click on "Upload"). Both returned 200.
5. **A console error appears at every sign-in:** `WebSocket error: Event`, from the first telemetry socket being
   closed before it connects; a new socket opens 0.1 s later and telemetry works. Invisible on camera.
6. **The "freshly opened project shows a red Refused badge" sentence in 2.4 did not hold at 3 drones.** The saved
   project opened with "Rebuild needed" and "Unverified · exports locked" and its stored 2 m 10.4 s timeline
   (`studio-6-unverified.png`). The sentence was about the 35-drone project; that size was not re-run.
7. **A New Show (empty stage) reads "SHOW LENGTH 1m 42.0s, VEHICLES 6, RATE 4 Hz"** above "Stage empty"
   (`studio-1-no-badge.png`). Not investigated; it may just be defaults.
8. **A locked (disabled) primary button still looks bright:** in `studio-1`, `studio-4` and `studio-5` the
   "Export Google Earth KML" and "Transmit to GCS Fleet" buttons are `disabled` but are still drawn in a saturated blue,
   while the JSON, CSV and `.tara` buttons are clearly dimmed. Not a safety issue (they do not respond); it can mislead on camera.

## Limits of what was verified

- **E-STOP** was verified as a command path on the kinematic sim: the first click only arms "CONFIRM KILL?"
  (no API call; fleet state still `RUNNING`), the second sent `POST /api/estop` (200), the fleet went
  `RUNNING > ESTOP > DISARMED` and `armed` fell from 3 to 0, with each drone card reading TERMINATED
  (`dashboard-11-before-estop.png`, `dashboard-12-estop-confirm.png`, `dashboard-13-after-estop.png`). Run twice with the same result. That the motors stop and an airborne drone falls is **NOT VERIFIED**: the sim
  has no motors, and E-STOP on PX4 or hardware was not run. After E-STOP the fleet state reads `DISARMED`, not a
  latched `ESTOP`.
- **`./tara reset`** was run on a stack whose supervisor I had killed with `kill -9`, leaving six orphans
  (2 uvicorn, 2 vite wrappers, 2 vite). `./tara status` listed them; `./tara reset` removed all six, freed ports
  5173, 5174, 8001 and 8090, and `./tara status` then said "nothing running". Before running it I listed every
  process its kill patterns match; all six were mine. **It does not reach processes inside Docker Desktop's VM:**
  at 12:40 a SITL container was up and the same patterns matched nothing on the host. It would kill another
  session's native stack (not run against one, by design).
- **Clean install caveats:** a fresh clone, a new `.venv`, `pip install -r requirements.txt` (163 s) and
  `npm install` in both web packages (4 s and 5 s), then `./tara doctor`. The pip and npm caches were warm
  and this machine already had Python 3.12.3, Node 22.23.3 and npm 10.9.9, with network access. A cold cache, a
  different OS image or offline use is **NOT VERIFIED**.
- The dashboard flight is T1 only (`tara_sim`, kinematic). It says nothing about Gazebo or PX4 behaviour.
- `dashboard-7-show-running.png` is scrolled to the planner panel; `dashboard-11-before-estop.png` shows the top of the page while the show is RUNNING (3/3 armed).

## Load, checked before each step (1-minute average, 16 cores; other sessions were running)

| Step | Load at start |
|---|---|
| Fresh clone install | 8.4 |
| Stack up, doctor, 3-drone build | 7.8 to 9.9 |
| Studio header states | 7.7 |
| Studio full-screen preview | 8.4 |
| Dashboard flight | 10.7 (rose to 16 for a while, from other sessions) |
| `kill -9` and `./tara reset` | 16.6 |
| E-STOP run | 7.3 |

A SITL container (`tara-sitl-171acc`) was running for part of this window, and the #171 slot was still marked
"taken" (last slot comment on #171: 2026-11-04T07:03Z). My work added at most one nice-19 stack of 3 drones plus
one headless browser at a time.
