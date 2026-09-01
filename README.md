# Taramandal

Taramandal is a drone light show platform: a choreography design studio and a
ground control station, built to scale from a handful of simulated drones up
to hundreds on a single machine.

Two products, one shared engine:

- **Studio** — design formations, sequence multi-formation shows, validate
  safety, export or transmit to a fleet.
- **GCS (Ground Control Station)** — connect to a fleet (real PX4/Gazebo or a
  vectorized simulator), launch/land/RTL, monitor live telemetry, and watch
  the show from three views: an operations dashboard, a georeferenced 3D
  globe, and a night-sky preview.

## Repo layout

```
packages/
  tara-core          shared safety limits, geometry, layout — Python
  tara-gcs           fleet manager: telemetry, safety monitors, playback — Python
  tara-sim           vectorized drone-fleet simulator (no PX4/Gazebo needed) — Python
  tara-studio        choreography engine: generators, validator, assignment — Python
  tara-web           GCS dashboard — React
  tara-studio-web    Studio design UI — React
apps/
  tara               the `tara` CLI orchestrator
sim/
  px4                PX4 + Gazebo SITL fleet launcher (real-flight-dynamics backend)
```

## Quickstart

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
(cd packages/tara-web && npm install)
(cd packages/tara-studio-web && npm install)

./tara doctor            # checks your environment first
./tara up --backend sim -n 20   # fleet + both APIs + both dashboards, one command
# Ctrl+C stops everything
```

`tara up --backend px4 -n 3` runs the same stack against real PX4 SITL +
Gazebo instead of the simulator (needs PX4-Autopilot built locally; see
`sim/px4/launch_fleet.sh`).

Once the GCS is up, the simulation itself can also be started and stopped from
the dashboard's Simulation Control Bar — pick a fleet size, hit **Launch Gazebo
SITL**, and watch the status pills — so running a show does not require a
terminal. `tara up` remains the developer path; the control bar is the operator
one. RViz can be launched the same way once a PX4 fleet is running.

Run the test suite:

```bash
pytest -m "not slow"     # full suite, ~20s
pytest                    # includes the 100-drone milestone test, ~90s
```

## Contributing

This repo is private and access is by invitation. If you've been added as a
collaborator, start with [CONTRIBUTING.md](CONTRIBUTING.md) — it covers scope,
branching, and how issues are assigned before you write any code.
