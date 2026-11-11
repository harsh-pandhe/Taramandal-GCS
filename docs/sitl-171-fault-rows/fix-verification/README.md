# #171: the denied-land case before and after the fleet-wide refusal fix

The same scenario (`denied_land_termination_clean`, script in `../rig/scenarios.py`: arm on the pad, flight termination, then `land`) run twice
on PX4 v1.16.2 + Gazebo SITL, 1 drone, headless, one fresh container per run, removed right after. Nothing else was running
(no other `tara-sitl-*` container, host 1-minute load 1.26 and 2.07 before the runs, Docker Desktop VM with 3.65 GiB).

| | `before-main/` | `after-fix/` |
|---|---|---|
| Code | `origin/main` code (commit 9dc0186: the docs branch, which has no code difference from `origin/main` in `packages/`) | commit 12df785, this PR's fix |
| Checks | FAIL, 6 / 7 | PASS, 7 / 7 |
| PX4 `land` direct | `COMMAND_DENIED` | `COMMAND_DENIED` |
| `POST /api/drone/0/land` | 502 | 502 |
| `POST /api/land` (fleet-wide) | **200** "Emergency landing commanded" | **502** "Fleet-wide landing refused by every connected drone (drone 0: COMMAND_DENIED ...). Nothing was commanded." with the per-drone breakdown |
| Fleet audit (`audit.jsonl`) | `STAGED -> LANDING` by the refused command, then `-> DISARMED` 0.07 s later (only because PX4 auto-disarmed after termination) | no transition; one refusal entry: `accepted: false`, `state: STAGED`, `refused_drones: {"0": "COMMAND_DENIED ..."}`, `fleet_state_advanced: false` |
| Fleet state after the call | `LANDING` (then `DISARMED`) | `STAGED` |

Only the ground case with one drone was run. NOT VERIFIED on PX4: the partial case (some drones accepting), RTL and E-STOP refusals, and a denied land while
airborne; those are covered by the unit tests in `packages/tara-gcs/tests/test_fleet_command_refusal.py` only (mock drones).

Kept per run: `run.json`, `result.json`, `scenario.log`, `timeline.csv`, `audit.jsonl`, `cmd_audit.jsonl`, `up.log`, `px4_console.tgz`. Left out to keep the PR small:
the PX4 ulogs, the cadence log and the container log (they are on the machine that ran it).
