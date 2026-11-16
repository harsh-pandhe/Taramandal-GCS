# T2 (PX4 SITL + Gazebo) recorded acceptance on `origin/main` 6e42b65 + #255, tree `335d84d`

Evidence for [#172](https://github.com/harsh-pandhe/Taramandal-GCS/issues/172) (3-drone Studio show) and
[#171](https://github.com/harsh-pandhe/Taramandal-GCS/issues/171) (single-drone hover and fault matrix) on one code tree.
No hardware claim. Run on 15-16 Nov 2026 (UTC) by Dhiraj while Harsh was unavailable; decisions on the two issues are
Dhiraj's, made against the issues' written acceptance criteria (see the end).

**The tree.** `origin/main` 6e42b65 merged with the head of PR #255 (ed3c132, per-show site profile). Tree hash
`335d84da65ed1052e7d85ed688feff75623ecd60`. A fresh `git merge-tree` of main and #255's head reproduces that hash, so a squash
merge of #255 onto this main gives exactly the tree that flew. (The flown commit is the local merge 06fb92c; the hash that
matters is the tree.) PX4 v1.16.2.

## A. Recorded takes: Gazebo + RViz + GCS dashboard visible (user-space rig)

Rig: PX4 SITL + Gazebo Sim 8.10 (user-space conda build, not the Docker image) + RViz, started by `tara up --backend px4 --gui`;
the dashboard in a browser; one screen recording per take. Every frame carries a label bar with the commit and drone count.
Host: 16 CPUs, load 2.6-5 during the takes; Gazebo real-time factor read 98.99-99.27 % in the frames I checked.
**Two of the six takes are published as redacted clips** in `docs/recordings/` (a hover take, `camp_n1_k2`, and a show take, `camp_n3_s2`; each with a `.notes.json` that gives the redaction method, the all-frames check and the privacy review). The RViz title bar in the originals shows a home-directory path, so the originals stay on the machine that recorded them (hashes in the notes), and the other four takes are not published as video. The GCS black box of every take is committed, and the numbers below are computed from it
(`analyse_take.py`, reproducible from the files here: `REPO=<checkout> python analyse_take.py show recorded-takes/camp_n3_s1 recorded-takes/show3.json`).

### A1. #172: three consecutive 3-drone passes

The show is the Studio-built Diwali 3-drone segment (rangoli ring, sphere burst, ring): Studio `/api/validate` returned
PASSED with `feasibility_certified: true` (0 violations of every kind, jerk 15 m/s3), `/api/export` was a real (not preview)
export, and the JSON was signed with Studio's key (`show3.json.sig.json`, key `7fa30d7106e1dfbf`, SHA-256 matches `show3.json`),
uploaded to the GCS (signature verified), and started with `execution="streamed"`. Predicted: 84.8 s, minimum separation 2.2 m,
maximum altitude 14.67 m (`show3.export-result.json`).

| Take | GCS lifecycle | Time RUNNING | Mean / max tracking error | Flown min separation | Max altitude flown | Final altitudes | Safety events |
|---|---|---|---|---|---|---|---|
| `camp_n3_s1` | STAGED, PREARM, ARMED, TAKEOFF, FORMATION_HOLD, RUNNING, SHOW_COMPLETE | 96.9 s | 0.05 / 0.19 m | 2.16 m | 14.68 m | 0.07, -0.02, 0.04 m | none |
| `camp_n3_s2` | same | 96.8 s | 0.05 / 0.19 m | 2.17 m | 14.68 m | 0.04, 0.01, 0.10 m | none |
| `camp_n3_s3` | same | 96.8 s | 0.05 / 0.18 m | 2.17 m | 14.68 m | 0.01, 0.02, 0.05 m | none |

RUNNING is the 84.8 s plan plus an 11.1 s start leg (the streamed start flies the drones from their hover points to the
show's first frame; offset found by minimising the error, identical in all three). Minimum separation flown is 0.03-0.04 m under
the planned 2.2 m, a finding in the sense of the issue (flown separation can sit slightly under the planned figure) but well above
the 2.0 m limit. Tracking and separation are measured from the GCS's own 10 Hz telemetry, not from Gazebo's model poses.

### A2. #171: three consecutive recorded single-drone hover passes

`POST /api/single-drone/hover-sequence` (target 1.0 m, 5 s dwell) with an authenticated FLIGHT_COMMANDER session, started by a
human-run script, not from the dashboard (the dashboard has no hover control). Gazebo, RViz and the dashboard were visible.

| Take | Endpoint | HOVER_REACHED | Dwell | Peak / time within 0.1 m of peak | Descent | Final altitude | Modes | Safety events |
|---|---|---|---|---|---|---|---|---|
| `camp_n1_k1` | 200 COMPLETED, 19.5 s | 0.86 m | 5.03 s | 0.99 m / 5.0 s | 0.61 m/s | -0.04 m | HOLD, LAND, OFFBOARD | none |
| `camp_n1_k2` | 200 COMPLETED, 19.4 s | 0.86 m | 5.04 s | 0.98 m / 5.0 s | 0.60 m/s | 0.05 m | HOLD, LAND, OFFBOARD | none |
| `camp_n1_k3` | 200 COMPLETED, 19.2 s | 0.86 m | 5.04 s | 0.99 m / 4.7 s | 0.61 m/s | 0.01 m | HOLD, LAND, OFFBOARD | none |

The climb is confirmed at 0.86 m against the 1.0 m target (inside the `max(0.15, 0.1 x target)` tolerance), then the drone
settles at about 1 m (peak 0.98-0.99 m); each take ends landed and disarmed.

These takes also flew #255's fleet-frame geofence monitor on real PX4 flight code; no geofence event occurred in any take.

## B. #171 fault matrix on the same tree (Docker rig, headless)

Same rig and scenarios as `docs/sitl-171-acceptance/` and `docs/sitl-171-fault-rows/` (scripts in the former's `rig/`), one fresh
container per run, PX4 v1.16.2 + Gazebo Sim 8.15, image `sha256:424c4648...92d3cb`, `tara up --backend px4 -n 1` (`gcs-kill` outside the
supervisor, as before). Records in `fault-matrix/<run>/` (`run.json` has every check; `up.log` omitted because it contains
local paths; PX4 ulogs and container logs stay on the machine). **Headless: no Gazebo window or recording for this table**; the
recorded half is section A.

| Row | Run | Verdict | Key measurement |
|---|---|---|---|
| Nominal, 3 consecutive | `nominal-1`, `-2`, `-3` | PASS x3 (7/7 each) | HOVER_REACHED 0.86 m, dwell 5.04-5.05 s, peak landing descent 0.71 / 0.72 / 0.64 m/s, 18.7-18.9 s |
| Manual takeover mid-hover | `takeover-dwell` | PASS (5/5) | sequence stopped 0.38 s after the switch (500 "operator took over"); PX4 stayed POSCTL, armed; GCS sent no LAND |
| Manual takeover during landing | `takeover-landing` | PASS (4/4) | PX4 stayed POSCTL, armed, airborne 25 s; 500 "operator took over during the landing" |
| Link loss mid-hover (PX4 killed) | `link-kill` | PASS (6/6) | `connected` 0 and `armed_stale` 1 after 0.26 s; hover fails closed, 500 "disconnected during hover dwell" |
| Reconnect | `reconnect-settled` | PASS (7/7) | frozen 8 s: `connected` 0 after 2.56 s, back 0.26 s after resume; hover after the estimate settled completes |
| Rejected command (hover before preflight green, PX4 in LAND) | `reject` | PASS (6/6) | 412 "pre-arm gates are red", names the flight mode; PX4 stayed disarmed |
| Cold-start rejected command | `cold-start-probe` | PASS (8/8) | hover and launch refused 412 while red (t=14.6 s), fleet stayed STAGED, 48 PX4 arm probes from boot, PX4 armed at 15.26 s together with the GCS going green, hover then completes |
| | `cold-start` | FAIL (6/7), superseded | the older `cold_start` scenario: its own `land` at cold start puts PX4 in LAND so preflight never turns green (known, documented in `docs/sitl-171-fault-rows/` as `cold-start-1`); kept, not deleted |
| PX4-denied `land` (on the ground, armed on the pad) | `denied-land` | PASS (7/7) | PX4 answers COMMAND_DENIED; single-drone and fleet-wide land return 502 with the reason; fleet state stayed STAGED. On 1d2d3a4 this row failed (fleet-wide land answered 200); PR #258 fixed it and it passes here |
| Abort and land mid-climb | `abort-climb` | PASS (4/4) | peak 1.64 m (target 3 m), landed and disarmed, hover 500 "yielding to fleet emergency state LANDING" |
| Second hover / upload during a hover | `guard` | PASS (6/6) | 409 on a second hover and on uploads by both roles; original hover completes; same upload accepted afterwards |
| Cancelled request (client disconnect) | `cancel` | PASS (5/5) | 409 while abandoned sequence runs, lands and disarms, GCS follows within 8 s, new hover accepted |
| Dead GCS mid-hover | `gcs-kill` | PASS (3/3) | PX4 left OFFBOARD after 2.0 s, disarmed on the ground after 8.0 s |
| Dropped telemetry stream | `stream-drop`, `stream-drop-hover` | PASS (5/5, 5/5) | `connected` 0 at once; back after 4.6 s (ground); mid-hover: 500, PX4 left OFFBOARD after 1.8 s, disarmed after 6.7 s |
| Armed-but-silent drone | `armed-silent` | PASS (6/6) | `armed` 0 and `armed_stale` 1 after 3.04 s; back to armed 1 / stale 0 0.26 s after resume |

### Measured cadences (the GCS's own MAVSDK connection, 17 runs, from the rig output)

- `landed_state()`: **periodic**, about 1 Hz (median interval 1.000 s; 99th percentile 1.0005-1.055 s in undisturbed runs; largest gap 1.06 s;
  up to 228 identical consecutive values, and the landing confirmation still completed: a steady ON_GROUND does not trip the 2.5 s staleness check).
- `position_velocity_ned`: **about 30 Hz** (29.86-30.01 Hz undisturbed; largest gap 0.095 s).
- Gaps above 2.5 s appear only in the runs where a freeze or kill was injected (`reconnect-settled` 8.2 s, `stream-drop*` 3.5-5.0 s, `armed-silent` 10.6 s).

## C. Test suite on the same tree

`scripts/ci-gates/python-tests.sh` (`pytest -p asyncio -m "not slow"`) on tree 335d84d: **2682 passed, 3 failed**, 7 deselected (16 min). The run shared the host with an unrelated project's test run (load average 10-14 on 16 CPUs), and the three failures are the timing-sensitive tests `test_validate_scale.py::test_validate_show_a_long_show_stays_cheap_per_waypoint`, `test_load_scale.py::TestSyntheticTelemetryLoad::test_proximity_scan_keeps_up_with_live_updates` and `test_gcs_death_stops_mavsdk_server.py::test_server_dies_with_the_gcs[SIGHUP]` (a known flake). Run again on their own, at load about 12, they passed on this tree and on plain `origin/main` (tree 8dcae83), twice each, alternating. The 14 tests of `test_site_profile.py` pass. Not run here: the frontend suites and the Playwright jobs (CI ran them green on #255's head ed3c132, and main's CI runs after the merge).

## D. What this does not show

- Takes in A use a non-stock Gazebo (user-space 8.10) and a local RViz config; the Docker rig in B uses Gazebo 8.15. Both are real PX4 flight code.
- The hover was started by an authenticated API call, not from the dashboard.
- The fault matrix (B) is headless; it was not screen-recorded. Its Gazebo window and RViz were not open.
- Not run: a PX4-denied `land` while airborne; a fault where the link stays up but a single telemetry field freezes; the real `CancelledError` path; more than one drone in the fault matrix.
- Stretch (about 10 drones) was not repeated here; earlier recorded 1 / 3 / 10-drone clips exist from before (PR #253, on older commits).
- 5-drone runs: 1 of 4 failed on f556537 with an unproven cause (PR #254); not re-run here.
- Simulation only. No hardware, no radio link, no real GPS.

## E. Decisions (Dhiraj, while Harsh is unavailable)

Written criteria vs this evidence are in the comments on #171 and #172.
