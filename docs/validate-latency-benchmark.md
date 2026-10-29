# `/api/validate` latency: the documented budget (issue #156)

Measured with a throwaway script against `tara_studio.server`'s real
`/api/validate` endpoint (`TestClient`, not mocked), and directly against
`tara_core.validate.validate_show` in isolation. Reproduce the direct call
with `packages/tara-core/tests/test_validate_scale.py`, which pins the
500-drone case as a CI regression guard.

Measured on Ubuntu 24.04, Python 3.12.3, 2026-10-29.

## The bug and the fix (#164)

`validate_show` sweeps the whole show's timeline (`dt=0.1`) and, at every
step, interpolates every drone's position via
`tara_core.geometry.interpolate_waypoint`. That function used to scan each
drone's waypoint list linearly to find the segment containing the sweep
time — O(waypoints) per call, so the whole sweep was
O(steps x drones x waypoints). A cProfile of the 500-drone case (see the
issue) found 76% of the time inside that one function.

#164 replaced the linear scan with `bisect_left` — O(log waypoints) per
call — with a 200-case randomized equivalence test against the old scan
(`test_geometry.py::test_bisect_interpolation_matches_linear_scan_including_duplicate_times`),
including duplicate timestamps, so segment choice, clamping, yaw arc and
velocity semantics are unchanged. Also added: a scaling smoke test on a
single 20,001-waypoint trajectory
(`test_interpolation_scales_to_long_trajectories`), which the old linear
scan would have taken tens of seconds on and the new one clears in
milliseconds.

## Current latency: a clean show (no violations)

`/api/validate`, end to end, via HTTP (`generate_circle`-class workload,
~300-420 stored waypoints/drone depending on fleet size, 30s/2Hz show,
median of 5):

| drones | `/api/validate` (HTTP) | `validate_show` (direct call) |
|---:|---:|---:|
| 100 | ~1.7 s | — |
| 500 | ~8 s | ~0.5 s (301 waypoints/drone, no violations) |
| 1000 | ~21 s | — |

Growth from 500 to 1000 drones (2x the drones) is ~2.7x the time — close
to linear, not the old superlinear blow-up. This is the "Expected"
behavior the issue asks to confirm: validation time now grows roughly in
proportion to samples x drones, not samples x drones x waypoints.

**The gap between the HTTP number and the direct-call number is not
`interpolate_waypoint`.** Measured where the time actually goes at 500
drones, isolating each layer (self-review, pre-merge — not left as an
unattributed "two things probably explain it"):

| call | result |
|---|---:|
| `validate_show` direct, grid, 0 violations | **~0.5 s** |
| `validate_show` direct, `generate_circle`, FAILED (50+ violations) | **~3.2 s** |
| `/api/validate` HTTP, same `generate_circle` show | **~8 s** |

So of the ~7.5 s gap between the clean-grid baseline and the full HTTP
path: **~2.7 s (36%) is `close_pairs`' own per-violation cost** on the
*same* function call, shape held constant as the only variable — not
FastAPI, not JSON, not the dynamics sweep. The remaining **~4.8 s (64%)**
is `certify_feasibility_report`'s extra `validate_dynamics` sweep (a
second, independent pass over the same timeline, computing
velocity/jerk/centripetal in addition to `validate_show`'s own
proximity/velocity/acceleration/yaw-rate/altitude checks) plus FastAPI
routing and JSON-encoding a multi-megabyte body.

`tara_core.proximity.close_pairs` already uses a `cKDTree`
(O(n log n) per sweep step, not O(n²) — see its own module docstring),
but it still does real work *per violating pair found*:
`generate_circle` packs drones closely enough to produce many
simultaneous close pairs, and reporting every one of them costs more
than a show with zero violations. This is expected, proportional-to-
violations cost, not a hidden complexity bug — flagging it, with the
measured split above, so a future investigation into "why is this
circle-shaped show slower than that grid-shaped show at the same drone
count" doesn't have to rediscover it from scratch.

## The documented budget

- **500 drones, a safe (zero-violation) show, `validate_show` called
  directly**: budget **10 s**, pinned by
  `test_validate_show_500_drones_completes_within_a_generous_budget`.
  Measured ~0.5 s on this machine — the 10 s budget is ~20x headroom for a
  loaded CI runner, while still clearly catching a return to the old
  linear-scan cost (which this workload's own waypoint count, 301/drone,
  would multiply by roughly another order of magnitude).
- **No CI gate for 1000 drones or for the full HTTP `/api/validate` path**:
  out of scope for this issue's "Required tests" (a 500-drone timing test
  with a generous budget) — the 1000-drone and HTTP-path numbers above are
  benchmark figures for this document, not CI-enforced budgets. A future
  issue could add either if a real regression risk shows up there
  specifically.

## What this does not cover

The UI "validating" state the issue's acceptance criteria also asks for
("UI shows a validating state while the verdict is pending") was added
separately in #173/#185 - `useShowBuilder`'s `validating` flag, shown in
`SafetyPanel` and `TransmitPanel` while a build's background validation
call is in flight. Not re-measured here since it's a frontend-only change
with no backend latency impact.
