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

## The missing piece: `drone_show_field.sdf`

The custom Gazebo world this project was built against — launch pads
`pad_0`..`pad_4` aligned to the GCS's own pad grid, plus a visual red safety
geofence ring — was created once (see `CHANGELOG.md`, v1.0.0) but was **never
committed** into this repo; it appears to have been lost in the move from the
original flat `scripts/`/`backend/`/`frontend/` layout to this monorepo.

Until a real copy is committed to `sim/px4/worlds/drone_show_field.sdf`:

- `launch_fleet.sh` refuses to start with a clear error, rather than silently
  falling back to a default Gazebo world that has none of the pads or the
  ring — that would make every spawn position the launcher computes
  (`PAD_SPACING_Y`, matching `tara_core.safety.DEFAULT_LIMITS.pad_spacing_m`)
  meaningless, and the discrepancy would be easy to miss.
- `tara doctor` reports `drone_show_field.sdf world: FAIL` for the same
  reason — this is the expected, honest state until the file exists.

Once a copy is placed at `sim/px4/worlds/drone_show_field.sdf`,
`launch_fleet.sh` installs it into
`$PX4_AUTOPILOT_DIR/Tools/simulation/gz/worlds/` automatically on every run —
no manual step needed beyond adding the file.

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
