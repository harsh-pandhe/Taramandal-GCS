# Design spike: sharded / multi-process fleet controller

**Status:** proposal, not scheduled. Do not implement until the load-test issue
proves single-process actually hits a wall below 1000 drones.

**Scope:** `tara-gcs`, `tara-sim`, and the parts of `tara-core` a shard split
would touch. Studio is unaffected.

## 1. Why this might be needed

Today one `uvicorn` process runs everything: the `DroneManager`, its per-drone
monitor tasks, the 10Hz proximity and geofence loops, the 10Hz trajectory
playback loop, and the WebSocket encoder — plus, on `backend="sim"`, the
`FleetState` physics tick. All of it on one asyncio event loop, and therefore
one CPU core.

`FleetState` itself is not the thing that worries me. It is genuinely
vectorized: one numpy tick for the whole fleet, so 1000 drones cost roughly what
10 do ([fleet_state.py:210](../packages/tara-sim/tara_sim/fleet_state.py#L210)).
The per-drone costs live on the `tara-gcs` side, and they are the ones that grow
linearly:

| Cost centre | Scaling | Where |
| --- | --- | --- |
| Monitor tasks | 8 asyncio tasks per drone → 8000 at n=1000 | [drone_manager.py:148-155](../packages/tara-gcs/tara_gcs/drone_manager.py#L148-L155) |
| Position monitor wakeups | n tasks × 20Hz = 20k wakeups/s | [system.py:80](../packages/tara-sim/tara_sim/system.py#L80) |
| Trajectory playback | n `interpolate_waypoint` calls + n awaited coroutines per 10Hz tick | [drone_manager.py:575-637](../packages/tara-gcs/tara_gcs/drone_manager.py#L575-L637) |
| Geofence loop | O(n) Python loop at 10Hz | [drone_manager.py:768](../packages/tara-gcs/tara_gcs/drone_manager.py#L768) |
| Proximity loop | O(n log n) tree build at 10Hz, but rebuilt from a Python list-of-lists every tick | [drone_manager.py:727-731](../packages/tara-gcs/tara_gcs/drone_manager.py#L727-L731) |
| Fast frame encode | n × 9 `struct.pack` floats at 20Hz | [telemetry_codec.py:63](../packages/tara-gcs/tara_gcs/telemetry_codec.py#L63) |

Two of these are worth calling out before anyone reaches for multiprocessing.

**The per-drone task fan-out is the likely bottleneck, and sharding is not the
only fix for it.** At n=1000 the sim backend spawns 8000 asyncio tasks whose
entire job is to copy numpy scalars into a dict, one drone at a time — the very
per-drone pattern `FleetState`'s docstring says it exists to avoid. A
`SimFleet`-aware bulk telemetry path (one task that slices all n rows out of the
arrays per tick and writes `DroneManager.telemetry` in one pass) would collapse
8000 tasks to ~2 without any process boundary. **If the load test fails, try
this first.** It is a much smaller change than anything below, and it is
possible that nothing in this document is ever needed.

Sharding earns its place only if the load test shows the remaining work —
playback, safety, encode — still saturates a core after that. This document
assumes it does.

## 2. What we are sharding, and what we are not

The unit of sharding is **the per-drone control and telemetry work**, not the
physics and not the show.

- **Sharded:** monitor loops, setpoint dispatch, telemetry dict maintenance.
- **Not sharded:** the show clock, the decision to arm/land/RTL, the trajectory
  file, the WebSocket to the browser. These stay in one coordinator process.

This split matters because the alternative — fully autonomous shards that each
run their own show — would give us N independently drifting show clocks, and a
light show whose halves are 40ms apart is a broken light show.

### Topology

```
                    ┌─────────────────────────────────┐
                    │  Coordinator  (tara-gcs)        │
                    │  • FastAPI + /ws/telemetry      │
                    │  • show clock, trajectory file  │
                    │  • fleet-wide RTL/land decision │
                    │  • cross-shard proximity        │
                    │  • unified telemetry mirror     │
                    └───┬────────────┬────────────┬───┘
                        │            │            │      shared memory (telemetry)
                        │            │            │      + a small control socket
                  ┌─────▼────┐ ┌─────▼────┐ ┌─────▼────┐
                  │ Shard 0  │ │ Shard 1  │ │ Shard 2  │
                  │ drones   │ │ drones   │ │ drones   │
                  │ 0..333   │ │ 334..666 │ │ 667..999 │
                  └──────────┘ └──────────┘ └──────────┘
```

Shard count defaults to `min(cpu_count() - 1, ceil(n / 250))`, floor of 1 — one
core reserved for the coordinator, and no shard split at all for the fleet sizes
we actually fly today. At n=100 this is a single shard, which means the sharded
code path *is* the normal code path and does not rot from disuse.

### Partitioning: by ID range, not by geography

Assign drone `i` to shard `i // ceil(n / shard_count)`.

The tempting alternative is spatial partitioning — put physically-near drones in
the same shard so most proximity checks stay local. I would not do it:

- Drones **move**, and a light show moves them a lot. A spatial partition
  requires rebalancing mid-show, which means migrating a drone's connection and
  monitor state between processes while it is flying. That is a lot of machinery
  and a lot of new failure modes for a safety-critical path.
- A formation's whole point is that drones are near each other. Spatial locality
  buys less than it does in, say, a particle simulation, because the interesting
  formations are precisely the dense ones.
- ID ranges are static, so a drone's shard is derivable from its ID with
  integer division — no lookup table, no migration, no rebalance.

Static ID ranges mean the shard split is invisible to `_pad_offset`, the
trajectory slot remapper, and the telemetry codec's sorted-ID ordering, all of
which already assume a contiguous `range(n)`.

## 3. Cross-shard proximity

This is the part that actually needs designing, and the safety-critical one.

The constraint: `DEFAULT_LIMITS.min_separation_m` applies to *every* pair in the
fleet, not just pairs that happen to land in the same shard. A partition by ID
gives no spatial guarantee whatsoever — drones 333 and 334 are in different
shards and may well be flying wingtip to wingtip.

### Do the proximity check in the coordinator, on shared memory

Rather than have shards exchange boundary data with each other, keep proximity
checking exactly where it is today — one process, one tree, whole fleet — and
make the data it reads cheap to get.

Each shard writes its drones' fleet-frame positions into a shared-memory
`numpy` array of shape `(MAX_FLEET_SIZE, 3)` (`multiprocessing.shared_memory`),
at the row indices it owns. No locking: each shard owns a disjoint row range and
is its only writer, float32 stores are atomic on the platforms we target, and a
reader that catches a torn frame sees a position at most one tick stale — which
is well inside the noise the safety margin already absorbs
(`position_noise_std_m` is deliberately non-zero for exactly this reason).

The coordinator then runs the same 10Hz loop it runs today, over the whole
array:

```python
positions = shared_positions[:n]              # zero-copy view, no per-drone Python
active = shared_armed[:n] & shared_connected[:n]
tree = cKDTree(positions[active])             # one tree, whole fleet, no boundaries
```

Three things fall out of this:

1. **There is no cross-shard proximity problem.** The check never sees the shard
   boundary, so a boundary case cannot be missed. Compare this to a
   ghost-zone/halo-exchange scheme, where every boundary width and every
   exchange interval is a chance to miss a collision — a failure mode that is
   silent until it isn't.
2. **It is faster than today's version**, not slower. The current loop rebuilds
   a Python list-of-lists from the telemetry dict on every tick
   ([drone_manager.py:727](../packages/tara-gcs/tara_gcs/drone_manager.py#L727));
   this reads a numpy view. The O(n log n) tree build was never the expensive
   part.
3. **The geofence check comes along for free** — it is a vectorized norm over
   the same array, replacing an O(n) Python loop.

If profiling later shows the tree build itself is the constraint at n=1000
(measure before believing it; scipy builds a 1000-point tree in well under a
millisecond), the escape hatch is to shard *the check* by spatial grid cell in
the coordinator with a halo of `min_separation_m` — but that is a later
optimization within one process, not a reason to distribute the check across
processes now.

### Why the failsafe must stay central

Both safety loops today respond to a breach with a **fleet-wide**
`trigger_rtl()` ([drone_manager.py:753](../packages/tara-gcs/tara_gcs/drone_manager.py#L753),
[:789](../packages/tara-gcs/tara_gcs/drone_manager.py#L789)) — every drone, not
just the offending pair. That is a whole-fleet decision and it cannot be made
independently by three processes that would each need to command drones they do
not own. The coordinator detects, decides, and broadcasts one `RTL` control
message to all shards; each shard executes it against its own drones.

The latency budget is worth stating: shared-memory write → 10Hz coordinator scan
→ control message → shard dispatch. Worst case roughly one scan period (100ms)
plus IPC, which is the same order as today's in-process path, where a breach is
also only noticed on the next 10Hz tick.

## 4. Unified telemetry for the dashboard

The dashboard must not learn that shards exist. `/ws/telemetry`'s contract —
one binary fast frame of `n × 9` float32 in sorted-drone-ID order, one JSON slow
frame ([telemetry_codec.py](../packages/tara-gcs/tara_gcs/telemetry_codec.py)) —
stays byte-for-byte identical, so `tara-web`'s decoder, EarthView, and
NightSkyView need **no changes at all**.

The mechanism follows from the proximity design: the fast-frame fields are
almost exactly the fields already in shared memory.

- Widen the shared array to hold all nine `FAST_FIELDS`
  (`fleet_{x,y,z}`, `local_{x,y,z}`, `heading`, `armed`, `connected`) as
  `(MAX_FLEET_SIZE, 9) float32`. This is 36KB at n=1000 — trivially small, and
  laid out in exactly the order `encode_fast_frame` emits.
- `encode_fast_frame` becomes `shared_fast[:n].tobytes()`. The current
  implementation builds a 9000-element Python list and `struct.pack`s it 20
  times a second; the sharded version is a memcpy. **This is the single biggest
  win in the whole design and, notably, it does not require sharding either** —
  it only requires the array. Another reason to try §1's bulk-telemetry path
  before reaching for processes.
- Slow-channel fields (battery, GPS, flight mode, latency, home, port) change at
  ~1Hz and are not worth putting in shared memory. Each shard pushes a dict of
  its own drones' slow fields to the coordinator once a second over the control
  socket; the coordinator merges the shards' dicts into the one it serves.
  Merging is a plain `dict.update` per shard because the key ranges are disjoint.

**Ordering:** the codec docstring notes that the fast frame's Nth record is the
Nth *sorted* drone ID, and that this coincides with the ID value only because
keys are a contiguous `range(n)`. Indexing shared memory by drone ID makes that
coincidence structural rather than accidental — row `i` is drone `i` by
construction — which is strictly more robust than what we have now.

**Partial-shard failure:** if a shard dies, its rows stop updating and the
coordinator serves stale positions with no indication anything is wrong. Every
shard must therefore write a heartbeat timestamp; a shard silent for >1s gets
its `connected` column zeroed by the coordinator, which the dashboard already
renders as disconnected, and which correctly excludes those drones from
proximity checks. Then the same failsafe path fires as for any other loss of
contact. This failure mode does not exist in the single-process design and is a
real cost of sharding — it belongs on the "reasons not to do this" side of the
ledger.

## 5. What changes, per package

### `tara-core` — no changes required

`MAX_FLEET_SIZE`, `launch_grid`, `altitude_layers`, `close_pairs`, and
`DEFAULT_LIMITS` are all pure functions or constants over the whole fleet. They
have no per-process state and are correct as-is. `proximity.close_pairs` keeps
working unchanged because the coordinator hands it the whole fleet.

*(If implementation later wants a numpy-array-native variant of `close_pairs`
to avoid the dict round-trip, that would be a `tara-core` change and must be
called out explicitly in that issue per CONTRIBUTING.md — it is not in scope
here.)*

### `tara-sim` — no changes to the physics

`FleetState` is already vectorized and already scales; nothing in its tick needs
to change. The only question is where it *lives*.

**Recommendation: keep one `FleetState` in one process.** It is a single numpy
tick for the whole fleet; splitting it across shards would divide work that
already costs almost nothing while adding N physics clocks to keep in sync.
Since a shard needs its drones' state to run monitor loops, this means either
(a) `FleetState` lives in the coordinator and writes shared memory that shards
read, or (b) `FleetState` lives in shard 0 and does the same. (a) is cleaner —
the coordinator already owns the show clock, so the physics tick and the show
clock stay in one process.

This makes the sim backend's shards thin: they own dispatch and monitoring, and
read physics from shared memory. It also means `SimSystem`'s duck-typed MAVSDK
surface is unchanged, so the "identical code path on both backends" property
([system.py:13-16](../packages/tara-sim/tara_sim/system.py#L13-L16)) survives
sharding — which is the property that makes the sim worth having.

Genuinely new to `tara-sim`: a shared-memory-backed accessor so a shard process
can read `position`/`velocity`/`armed` for its own ID range without owning the
`FleetState` object. Additive; the in-process path stays.

### `tara-gcs` — where the work is

**Unchanged:**
- Every public HTTP endpoint in `main.py`, request and response shapes included.
- The `/ws/telemetry` wire format.
- `_pad_offset`, `DEFAULT_ALTITUDES`, and the fleet-frame conversion.
- `remap_trajectory_to_healthy_drones`, `verify_launch_geometry`,
  `interpolate_waypoint` — all whole-fleet operations that stay in the
  coordinator.
- `telemetry_log` / `export_telemetry_csv` — the recorder samples the
  coordinator's unified telemetry mirror at 0.5Hz, so it sees the same thing it
  sees today.

**Changed:**
- `DroneManager` splits along a seam it does not currently have. Roughly:
  `FleetCoordinator` (owns the show clock, active trajectory, safety loops,
  telemetry mirror, WebSocket) and `ShardWorker` (owns `self.drones` for its ID
  range, the monitor loops, and setpoint dispatch). The existing `DroneManager`
  becomes the single-shard-in-process case, which is what keeps the
  n≤100 path — and the entire existing test suite — honest.
- Trajectory playback splits: the coordinator advances the show clock and
  broadcasts `elapsed`; each shard interpolates and dispatches setpoints for its
  own drones. Interpolation is per-drone work and belongs in the shard;
  the clock is fleet-wide and must not be duplicated.

  The subtlety: shards must not each free-run their own 10Hz timer, or they
  drift apart. The coordinator broadcasts an authoritative `elapsed` per tick
  and shards interpolate against *that value*, never against their own
  `event_loop.time()`. Shard-to-shard timing skew then equals IPC jitter, not
  accumulated clock drift.
- `launch_sequence`'s `LAUNCH_BATCH_SIZE = 5` becomes per-shard, so effective
  launch concurrency is `5 × shard_count`. Worth re-checking against the real
  PX4 backend — the batch size exists to bound gRPC/CPU load, and that limit is
  per-machine, not per-process.

**New:** a shard supervisor (spawn, health-check, restart-or-failsafe), the
shared-memory layout module, and the control-message protocol.

### `apps/tara` — one flag

`tara up` gains `--shards N` (default: auto per §2), passed to the GCS process
as an env var alongside the existing `NUM_DRONES` / `VEHICLE_BACKEND`
([services.py:90-92](../apps/tara/tara_cli/services.py#L90-L92)). `--shards 1`
must remain a supported, tested configuration forever.

### `tara-web` — no changes

Stated explicitly because it is the design's main constraint: if any part of
this requires a frontend change, the telemetry unification in §4 has failed.

## 6. Testing approach

Deliberately brief — this is a spike, and the real test plan belongs to the
implementation issue. The load-bearing points:

- Determinism first. The existing suite runs against `backend="sim"` with a
  seeded RNG; a sharded run at `--shards 1` must produce results identical to
  the unsharded run, and a `--shards 3` run must produce the same *safety
  outcomes* (same breaches detected, same failsafes fired) even if float
  ordering differs.
- A cross-boundary proximity test is mandatory: place drones `k` and `k+1`
  either side of a shard boundary, walk them together, assert the breach fires
  with the same latency as an intra-shard pair. This is the case the design
  exists to get right.
- Shard-death test. Kill a shard process mid-show, assert its drones go
  `connected=False` within the heartbeat window and the failsafe fires. This has
  to cross a real OS process boundary — an in-process test that never spawns a
  second process does not exercise the thing that can actually break. (Issue
  #4's restart test uses this pattern; reuse its spawn/kill approach once it
  lands.)
- The load test that motivated this spike becomes the acceptance test: same
  scenario, `--shards 1` vs `--shards N`, showing the wall actually moves.

## 7. Recommendation

Ordered, and the ordering is the point:

1. **Do not implement this yet.** It is contingent on the load test.
2. **If the load test fails, do §1's bulk telemetry path and §4's `tobytes()`
   fast-frame encode first.** They remove the two largest per-drone costs
   (8000 tasks, 20Hz Python-list encoding) with no process boundary, no IPC, no
   new failure modes, and no supervisor. My honest expectation is that this is
   enough to reach 1000 on the sim backend.
3. **Only if it is still short, shard** — with static ID ranges, central
   proximity over shared memory, and a central show clock.

The reason for that ordering: sharding introduces a genuinely new failure mode
(a shard dying with the rest of the fleet airborne, §4) into a safety-critical
system. That is a real cost, and it should be paid only against a measured
need — which is exactly what the load-test issue is for.

Worth noting that the sharding work is not wasted even if we never split
processes: the shared-memory array in §3–§4 is what makes both the fast path and
the safety loops cheap, and it is useful on its own.
