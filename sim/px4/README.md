# PX4 SITL + Gazebo backend

`./tara up --backend px4 -n <N>` runs the fleet against real PX4 SITL and
Gazebo Sim instead of the vectorized simulator, for real flight-dynamics
testing. This directory holds the launcher (`launch_fleet.sh`) and its
cleanup script (`stop_fleet.sh`); `tara doctor` checks most of what's below
before you waste time on a failed launch.

## Known-good PX4 version: v1.16.2

Pin your PX4-Autopilot checkout to tag `v1.16.2`:

```bash
cd ~/PX4-Autopilot
git checkout v1.16.2
```

Newer PX4 `main` (as of 2026-10) has two problems found while doing the first
Gazebo acceptance run (issue #177):

- It moved `gz_env.sh` from `build/px4_sitl_default/rootfs/gz_env.sh` to
  `build/px4_sitl_default/gz_env.sh`. `launch_fleet.sh` looks in both
  locations, so this alone isn't a blocker either way.
- Its barometer stream times out against Gazebo Sim 8.15. This one has **no
  workaround** — stick to v1.16.2 until it's investigated further.

`tara doctor` reports the installed tag (best-effort — it shells out to `git
describe --tags --exact-match`; an untagged or detached checkout is flagged,
not silently accepted).

## Building PX4 SITL without sudo

PX4's own `Tools/setup/ubuntu.sh` wants to use `sudo apt install` for every
dependency, which isn't available in every environment. If the following are
already installed system-wide, a sudo-free build works:

- A C/C++ toolchain, `cmake`, `ninja`, `ccache`.
- `gz-sim8-dev` (Gazebo Sim 8's development package) and the rest of the
  Gazebo Sim 8 runtime.

Then, from the PX4-Autopilot checkout root:

```bash
# PX4's Python build tooling (empy, jinja2, pyros-genmsg, etc.) into its own
# venv, so it doesn't fight your project venv's own dependency versions.
python3 -m venv .venv-px4
.venv-px4/bin/pip install -r Tools/setup/requirements.txt

# PX4's submodules include NuttX (the bare-metal RTOS for real flight
# controller hardware), which this project never flashes or builds — SITL
# doesn't need it. A full `git submodule update --init --recursive` clones it
# anyway and can fail/hang on a slow network for no benefit here. Cloning only
# a placeholder tag keeps the submodule present (PX4's build expects the
# path to exist) without pulling NuttX's own history.
git submodule update --init --recursive --depth 1 -- Tools/sitl_gazebo 2>/dev/null || true

source .venv-px4/bin/activate
make px4_sitl gz_x500
```

Adjust the submodule list to whatever your PX4 tag actually needs for a pure
SITL build; the point is skipping NuttX, not an exact command to copy
verbatim.

## `drone_show_field.sdf`

The custom Gazebo world this project was originally built against — launch
pads `pad_0`..`pad_4` aligned to the GCS's own pad grid, plus a visual red
safety geofence ring — was created once (see `CHANGELOG.md`, v1.0.0) but was
**never committed** into this repo; it appears to have been lost in the move
from the original flat `scripts/`/`backend/`/`frontend/` layout to this
monorepo, and its exact historical geometry is unknown.

Rather than guess at that lost geometry, `sim/px4/generate_world.py`
**generates** a new `sim/px4/worlds/drone_show_field.sdf` mechanically, from
this project's own authoritative constants — not invented numbers:

- **Pad positions**: `tara_core.layout.launch_grid(MAX_FLEET_SIZE)`, the
  exact same function `tara_gcs.drone_manager._pad_offset` indexes into for
  every real drone's fixed pad. For drone ids below the grid's column cap
  (`floor(geofence_radius_m / pad_spacing_m)`, 13 at current defaults), this
  is provably a plain Y-axis line in the fleet frame. Fleet Y is east (the
  fleet frame is NED) and Gazebo's world frame is ENU, so the pads are placed
  along Gazebo's **X** axis — which is exactly where `launch_fleet.sh` spawns
  PX4 SITL instances (issue #172 F1: both previously used Gazebo's Y, i.e.
  north, while the GCS assumed east). `generate_world.py` emits precisely
  that many pads, and fails loudly (not silently) if a future constant change
  ever breaks the line-property assumption.
- **Geofence ring**: `tara_core.safety.DEFAULT_LIMITS.geofence_radius_m`
  (30m), rendered as a ring of small visual-only markers (SDF has no annulus
  primitive).
- **Everything else** (physics, gravity, scene, ground plane, lighting,
  spherical coordinates) is copied verbatim from PX4's own real, upstream
  default world (`PX4/PX4-gazebo-models`, `worlds/default.sdf`) — cross-
  checked against the actual upstream source, not reconstructed from memory
  of SDF syntax.

Regenerate after any pad-spacing or geofence-radius change (issue #167 is
exactly this kind of change) with:

```bash
PYTHONPATH=packages/tara-core python3 sim/px4/generate_world.py
```

`launch_fleet.sh` installs whatever is at `sim/px4/worlds/drone_show_field.sdf`
into `$PX4_AUTOPILOT_DIR/Tools/simulation/gz/worlds/` automatically on every
run; `tara doctor` checks the file exists. If it's ever missing (a clean
checkout before the file has been generated, or it was deleted), both fail
with a clear, actionable error rather than silently falling back to a
default Gazebo world with none of the pads or the ring.

**Validated** (issue #177 follow-up, PR #200, 2026-10-30): beyond well-formed
XML and field-by-field cross-checking against PX4's own real default world,
this file has been load-tested against a real Gazebo Sim 8.15 instance
(`gz sim -s -r --headless-rendering ... --iterations 100`, clean exit, zero
errors) and a real PX4 v1.16.2 spawn replicating `launch_fleet.sh`'s exact
env vars — the drone landed at `(0, 0, 0.2)`, exactly `launch_grid(
MAX_FLEET_SIZE)[0]`, with a valid EKF home lock and normal mavlink streaming.
The generator's logic (pad math, XML structure) is unit tested; the file's
Gazebo-loadability is now confirmed first-hand, not just inferred.

## Node.js version

`tara-web` and `tara-studio-web` both pin `vite ^8.1.1`, which requires Node
`^20.19.0 || >=22.12.0`. A distro-apt Node (18.19.1 on Ubuntu 24.04, as of
this writing) has `npm` on `PATH` and will pass a check that only looks for
`npm` — but Vite's CLI crashes on startup with `ReferenceError: CustomEvent
is not defined`, and `tara up`'s supervisor treats that as a fatal service
death, tearing down the whole stack including the already-healthy PX4/
Gazebo/GCS side. `tara doctor` checks the actual Node version, not just that
`npm` exists, to catch this before `tara up` does. If you hit this, install a
newer Node — e.g. via NodeSource: `curl -fsSL
https://deb.nodesource.com/setup_20.x | sudo -E bash -` then `sudo apt-get
install -y nodejs`.

## `GZ_IP`

`launch_fleet.sh` exports `GZ_IP=127.0.0.1` before spawning. PX4's own `make
px4_sitl gz_x500` target sets this for you; this launcher spawns the `px4`
binary directly instead (to control per-drone spacing and pad offsets) and
has to set it explicitly, or Gazebo's multi-instance transport discovery can
fail to find the other instances.

## `tara doctor` coverage

With `backend = "px4"` in `tara.toml` (or `--backend px4`), `tara doctor`
checks: the PX4 binary is built, the world file above exists, the installed
PX4 tag, `gz_env.sh` is present (either path), and that the real Gazebo Sim
`gz` binary — not a ROS-vendored one — resolves on `PATH`.
