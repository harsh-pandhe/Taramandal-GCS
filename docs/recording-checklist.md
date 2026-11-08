# Human-only recording checklist (#134)

Under #134, for [#132](https://github.com/harsh-pandhe/Taramandal-GCS/issues/132), [#171](https://github.com/harsh-pandhe/Taramandal-GCS/issues/171) and [#172](https://github.com/harsh-pandhe/Taramandal-GCS/issues/172).
Written 2026-11-04 against `main` at `d7e7b2e`. Commands and expected results come from
[docs/diwali-runbook.md](diwali-runbook.md) (section numbers below); where the runbook has no expected result
because nobody has seen it, this page says so instead of inventing one. Nothing here was run to write it.

**Rule for every clip:** it says its tier on screen or in a title card (`plan`, `T1` or `T2`), and says
"simulation". There is no T3 clip. Never splice in a Gazebo frame from another run.

## What only a human can do

| Item | Why it cannot be done from an agent session | Who decides / owns | State |
|---|---|---|---|
| Gazebo + RViz + GCS dashboard recording (T2) | Needs a machine with a display, native PX4 v1.16.2 + Gazebo Sim 8, and ROS 2 Jazzy with `ros_gz_bridge` and `rviz2`. The container rig is headless and has no ROS 2 (runbook 4.2) | Harsh names the machine and runs RViz | **Not done as specified.** Three local, non-stock-build clips on `d7e7b2e` (1, 3 and 10 drones) exist in `docs/recordings/` (see [evidence pack](evidence-pack.md), Recordings row; the 10-drone clip shows 3 custom formations Studio cannot author; redaction checked every title bar and 6 s contact sheets, not every frame by eye). They are not a recording by the named person on a named machine, and whether they count for T2 is Harsh's call |
| Operator walkthrough of the Studio full-screen preview | The automated tour in `docs/recordings/` does not open the preview or export and uses canned API answers ([evidence pack section 9](evidence-pack.md#9-134-acceptance-audit)) | Vishal (proposed, runbook 5.1) | Not done |
| Music track and licence (#132) | A person picks a track and records its licence; nothing is chosen. A silent clip is the default (runbook 4.3) | Harsh approves the source | Not done |
| Hardware gate (#135) | A real hover needs Harsh's sign-off on airframe, bench checks, a **named** RC pilot and a permitted site; the firmware (PX4 or ArduPilot) is undecided (runbook 1, 5.3) | Harsh | Not scheduled; clips say "simulation" |
| Review before external sharing | #134's last acceptance line | Harsh | Not done |

## Recording 1: Gazebo + RViz + GCS dashboard (T2, 3 drones)

Before you start (runbook 2.1, 4.1):

1. Read the latest slot comment on #172. If it says "SITL slot taken", wait. Post "SITL slot taken (who, why)".
2. `git fetch origin && git checkout <sha> && git rev-parse --short HEAD`. Note the SHA; it goes in the file name.
3. `./tara doctor`. **Expect:** every line `[OK]`, including the PX4 and `gz` lines (on the container rig they fail, which is why this must be a native machine).
4. One-time setup, runbook 2.2: `./tara keygen $D/studio.pem --signer "Diwali demo"`, paste the printed line into `[security].trusted_keys` in `$D/tara.toml`, `python -m tara_gcs.auth add demo --role flight_commander` (passphrase of 12+ characters), then the `export GCS_CONFIG_PATH=... STUDIO_SIGNING_KEY=...` block. Pass the passphrase as `TARA_PASSPHRASE`, never `--passphrase`.
5. `tools/diwali_cpu_check.sh --wait`. **Expect:** exit 0, "idle" (no `tara-sitl-*` container, 1-minute load under 4).

Run:

6. Terminal 1: `./tara up --backend px4 -n 3 --gui`. **Expect:** GCS, Studio and both web servers answer and a Gazebo window opens. The runbook has not seen this window (**NOT VERIFIED**); if the world is empty or the three drones are missing, stop and report it.
7. Terminal 2: `sim/ros/launch_rviz.sh` (needs `$DISPLAY`; defaults `-w drone_show_field -p taramandal`, the values `sim/px4/launch_fleet.sh` uses). **Expect:** RViz shows the fleet. Also **NOT VERIFIED**. The script's own header still names `scripts/launch_fleet.sh`, which no longer exists (it is `sim/px4/launch_fleet.sh`).
8. Arrange the screen: GCS dashboard `http://localhost:5173` in a **1440x900** window, Gazebo and RViz beside it. Sign in as `demo`. **Expect:** the header shows the px4-sitl label, and `curl -s localhost:8090/api/status` says `fidelity: px4-sitl` (runbook 1, row D). No red banner in frame; the deviation panel is green.
9. Show the tier: a title card or a sentence, "T2, PX4 SITL and Gazebo, simulation, commit `<sha7>`".
10. Start the screen recorder. `tools/record/record_desktop_gazebo.sh <name> <seconds> :0.0` exists but has **not been run**: it needs `ffmpeg` and X11, records the whole display (not a 1440x900 window), and writes under `docs/recordings/`, which is tracked, so do not `git add` the clip. A recorder you trust is fine.
11. Terminal 3, from the repo root: `TARA_PASSPHRASE=<passphrase> python3 tools/sitl_planned_vs_actual.py run --show examples/diwali/segments/diwali_3drone_segment.json --operator demo --out ~/diwali-recordings/runs --label rec1` (the same arguments `tools/diwali_t2_sitl.sh` passes). It certifies, signs, uploads, launches and starts the show streamed, then lands.
    **Expect** (3-drone runs on the container rig, runbook 2.5, so a native machine may differ): launch to `FORMATION_HOLD` 10.2 to 10.7 s; start transient 0.01 to 0.02 s, **no descent at the start** (do not mention one); max tracking error 0.29 to 0.32 m; Gazebo min separation 2.155 to 2.176 m; real-time factor 0.997 to 0.998; show about 84 s after an 8.0 s start leg (#220); ends `DISARMED`; the tool prints PASS.
12. Stop the recording after `DISARMED`. Then check every drone, because the tool's PASS cannot see a drone that never flew: `python3 -c "import json,sys; d=json.load(open(sys.argv[1]))['actual']['gazebo']['tracking_error']; print({k:v['max_m'] for k,v in d.items()})" ~/diwali-recordings/runs/rec1.json`. **Expect:** every value under 1.5 m (clean runs read 0.28 to 0.83 m; a drone left on the pad reads 8 to 10 m).

**Discard the take and rerun, and do not count it,** if any drone aborts or RTL fires (one drone aborting ends the take for all), real-time factor is under 0.9 (the tool marks it degraded), Gazebo poses stall for more than 2 s, the deviation panel goes red, or step 12 shows a drone above 1.5 m (runbook 5.2).

After: Ctrl+C the `./tara up` terminal, stop RViz, post "SITL slot free" on #172. Name the clip `diwali2026_T2_3drones_gcs-gazebo-rviz_<YYYYMMDD>_<sha7>.<ext>` and keep `rec1.json` next to it (runbook 4.3). Clips stay out of git; the shared location is Harsh's decision and does not exist yet.

## Recording 2: Studio full-screen preview (tier `plan`: playback, nothing flew)

1. Runbook 2.3 step 1: `DIWALI_SIZES="35 100" tools/diwali_run_all.sh $D/dist` (about 1 minute). Then `./tara up --backend sim -n 35` with the 2.2 environment exported.
2. `node tools/diwali_to_vault.mjs --storyboard examples/diwali/storyboard_35.json --show $D/dist/diwali_35.json --name "Diwali 2026 (35 drones)"`.
3. In a 1440x900 browser window at `http://localhost:5174`: open the project from Project Vault. **Expect** a red **Refused** badge and a 0.00 s timeline until the next step (runbook 2.4).
4. Press **GENERATE SEQUENCE**. **Expect** the header badges **Built** (about 209.8 s at 35 drones, 354.6 s at 100) and **Safety passed · exports enabled**. If it says `Not flight-certified · exports locked`, `Safety failed · exports locked` or `Unverified · exports locked`, stop: this is not a certified show (the state table is in runbook 2.4).
5. Click **FULL-SCREEN PREVIEW**, press Play, let it run, press `Esc`. **Expect** the sidebar and console hidden, the camera kept, `Esc` exits (measured with Playwright; the frame rate on a real GPU is **NOT VERIFIED**, runbook 2.4).
6. Say or show "plan playback, nothing flew". No music unless Harsh has recorded a licence.

Name it `diwali2026_plan_35drones_studio-fullscreen_<YYYYMMDD>_<sha7>.<ext>`.
