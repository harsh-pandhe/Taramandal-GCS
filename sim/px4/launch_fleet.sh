#!/usr/bin/env bash

# Exit immediately if a command exits with a non-zero status
set -e

# Default parameters
NUM_DRONES=3
HEADLESS=0
PX4_AUTOPILOT_DIR=${PX4_AUTOPILOT_DIR:-"$HOME/PX4-Autopilot"}
# Hard ceiling. Real PX4 SITL is heavy (~one process + physics load per drone),
# so the practical single-machine limit is well below this — beyond ~10-15 drones
# use the synthetic-telemetry load harness instead of real SITL. Override with
# MAX_DRONES if you know your machine can take it.
MAX_DRONES=${MAX_DRONES:-50}
# Y-axis spacing between launch pads (meters). MUST match LAUNCH_PAD_SPACING_Y_M
# in backend/drone_manager.py (tara_core.safety.DEFAULT_LIMITS.pad_spacing_m)
# so the GCS fleet-frame math lines up with spawns. Not an integer (issue
# #167), so the offset below is computed with awk, not bash arithmetic.
PAD_SPACING_Y=${PAD_SPACING_Y:-2.2}

# Home/origin for the simulated world. PX4 SITL has no real GPS receiver: it
# synthesizes GNSS from the simulator's world origin, and if that origin is
# never set, the EKF has nothing to anchor a global position to. The visible
# symptom is the home-position lock bug this project has been carrying —
# `telemetry.health()` reports is_home_position_ok == false indefinitely, so
# arming is rejected and GLOBAL-mode trajectories have no home_lat/home_lon to
# offset against (drone_manager._monitor_home never receives a home message,
# leaving home_lat/lon at 0.0, which start_trajectory then skips setpoints on).
#
# Setting these three exports before spawning gives every instance a
# deterministic origin, so home latches within a second or two of boot instead
# of never. Values default to the PX4 SITL standard Zurich origin; override for
# a real show site so the georeferenced Earth view matches the actual field.
PX4_HOME_LAT=${PX4_HOME_LAT:-47.397742}
PX4_HOME_LON=${PX4_HOME_LON:-8.545594}
PX4_HOME_ALT=${PX4_HOME_ALT:-488.0}

# Parse options
while getopts "n:hp:" opt; do
  case $opt in
    n)
      NUM_DRONES=$OPTARG
      ;;
    h)
      HEADLESS=1
      ;;
    p)
      PX4_AUTOPILOT_DIR=$OPTARG
      ;;
    *)
      echo "Usage: $0 [-n <num_drones>] [-h] [-p <px4_autopilot_dir>]"
      exit 1
      ;;
  esac
done

# Validate that NUM_DRONES is a positive integer before any numeric comparison
# (a non-numeric -n value would otherwise crash the [ -lt ] test with a bash error).
if ! [[ "$NUM_DRONES" =~ ^[0-9]+$ ]]; then
  echo "Error: Number of drones must be a positive integer (got '$NUM_DRONES')."
  exit 1
fi

if [ "$NUM_DRONES" -lt 1 ] || [ "$NUM_DRONES" -gt "$MAX_DRONES" ]; then
  echo "Error: Number of drones must be between 1 and $MAX_DRONES (override with MAX_DRONES=)."
  exit 1
fi

if [ "$NUM_DRONES" -gt 10 ]; then
  echo "WARNING: Launching $NUM_DRONES real PX4 SITL instances is CPU/physics-heavy."
  echo "         Expect slow arming / EKF settling on typical dev hardware past ~10 drones."
fi

if [ ! -d "$PX4_AUTOPILOT_DIR" ]; then
  echo "Error: PX4 Autopilot directory not found at $PX4_AUTOPILOT_DIR"
  echo "       Set PX4_AUTOPILOT_DIR to your PX4-Autopilot checkout, or pass -p <dir>."
  exit 1
fi

echo "========================================="
echo "Starting multi-drone SITL simulation..."
echo "  Number of drones: $NUM_DRONES"
echo "  Headless mode: $HEADLESS"
echo "  Autopilot Path: $PX4_AUTOPILOT_DIR"
echo "  Home origin:    $PX4_HOME_LAT, $PX4_HOME_LON @ ${PX4_HOME_ALT}m"
echo "========================================="

# Call cleanup first
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
bash "$SCRIPT_DIR/stop_fleet.sh"

# Trap SIGINT, SIGTERM, and EXIT to run cleanup on termination
cleanup() {
  # Capture the real exit status immediately — every line below would
  # otherwise overwrite $? before the final `exit`, which used to mask a
  # failure after this point (e.g. a missing world file or gz_env.sh, issue
  # #177) as a misleading "exited with code 0" in the GCS's own process log.
  local exit_code=$?
  # Disable trap to avoid recursion on exit
  trap - SIGINT SIGTERM EXIT
  echo ""
  echo "========================================="
  echo "Stopping fleet and cleaning up background processes..."
  bash "$SCRIPT_DIR/stop_fleet.sh"
  echo "========================================="
  exit "$exit_code"
}
trap cleanup SIGINT SIGTERM EXIT

# The custom show world (launch pads pad_0..pad_4 + the visual geofence ring,
# see CHANGELOG 1.0.0) is not committed to this repo yet — lost in the
# monorepo move (issue #177). If a copy has been added at sim/px4/worlds/,
# install it into the PX4 checkout; otherwise refuse to start rather than
# silently falling back to PX4's own default world, which has none of the
# pads or the ring and would make every spawn offset below meaningless.
WORLD_DEST_DIR="$PX4_AUTOPILOT_DIR/Tools/simulation/gz/worlds"
WORLD_SRC="$SCRIPT_DIR/worlds/drone_show_field.sdf"
WORLD_DEST="$WORLD_DEST_DIR/drone_show_field.sdf"
if [ ! -f "$WORLD_DEST" ]; then
  if [ -f "$WORLD_SRC" ]; then
    echo "Installing drone_show_field.sdf into $WORLD_DEST_DIR..."
    mkdir -p "$WORLD_DEST_DIR"
    cp "$WORLD_SRC" "$WORLD_DEST"
  else
    echo "Error: drone_show_field.sdf not found at $WORLD_SRC or $WORLD_DEST."
    echo "       This custom world (launch pads pad_0..pad_4 + geofence ring) is"
    echo "       not yet committed to this repo (issue #177). See sim/px4/README.md."
    exit 1
  fi
fi

# Source Gazebo environment. PX4 main moved this file in 2026-10 from
# build/px4_sitl_default/rootfs/gz_env.sh to build/px4_sitl_default/gz_env.sh
# (issue #177); support both so the pinned v1.16.2 (old layout) and a newer
# checkout both work.
echo "Sourcing Gazebo Sim environment..."
GZ_ENV_OLD="$PX4_AUTOPILOT_DIR/build/px4_sitl_default/rootfs/gz_env.sh"
GZ_ENV_NEW="$PX4_AUTOPILOT_DIR/build/px4_sitl_default/gz_env.sh"
if [ -f "$GZ_ENV_OLD" ]; then
  source "$GZ_ENV_OLD"
elif [ -f "$GZ_ENV_NEW" ]; then
  source "$GZ_ENV_NEW"
else
  echo "Error: gz_env.sh not found at $GZ_ENV_OLD or $GZ_ENV_NEW."
  echo "       Build PX4 SITL first: (cd $PX4_AUTOPILOT_DIR && make px4_sitl gz_x500)"
  exit 1
fi

# Export correct world name
export PX4_GZ_WORLD=drone_show_field

# Isolate Gazebo partition to avoid conflicts with other simulations
export GZ_PARTITION=taramandal

# Anchor Gazebo's own network binding. PX4's own `make px4_sitl gz_x500`
# target sets this; this launcher spawns the px4 binary directly instead and
# must set it too, or multi-instance Gazebo transport discovery can fail
# (issue #177 item 2).
export GZ_IP=127.0.0.1

# Disable interactive PX4 shell to prevent EOF infinite loops in log files
export NO_PXH=1

# Anchor the simulated GNSS origin (see PX4_HOME_* above). Exported here rather
# than set per-instance on the px4 command line so every drone in the fleet
# shares one origin — a per-instance origin would put each drone in its own
# global frame and make fleet-frame GPS math meaningless.
export PX4_HOME_LAT
export PX4_HOME_LON
export PX4_HOME_ALT

# Export Headless if requested
if [ "$HEADLESS" -eq 1 ]; then
  export HEADLESS=1
fi

# Change directory to PX4 Autopilot root to resolve relative configuration paths
cd "$PX4_AUTOPILOT_DIR"

# Spawning instances
for ((i=0; i<NUM_DRONES; i++)); do
  # Calculate horizontal spacing offset along the Y axis (must match the GCS
  # fleet-frame pad offset — see PAD_SPACING_Y above). awk, not bash
  # arithmetic, since PAD_SPACING_Y is not an integer.
  Y_OFFSET=$(awk -v s="$PAD_SPACING_Y" -v i="$i" 'BEGIN { printf "%g", s * i }')
  
  if [ "$i" -eq 0 ]; then
    echo "Launching host drone (instance 0) at Y_OFFSET = 0m..."
    PX4_SYS_AUTOSTART=4001 PX4_SIM_MODEL=gz_x500 PX4_GZ_MODEL_POSE="0,0,0.2,0,0,0" ./build/px4_sitl_default/bin/px4 -d -i 0 > /tmp/px4_0.log 2>&1 &
    
    # Wait for gazebo server and host instance to settle down
    echo "Waiting 10 seconds for Gazebo Sim server to initialize..."
    sleep 10
  else
    echo "Launching follower drone (instance $i) at Y_OFFSET = ${Y_OFFSET}m..."
    PX4_GZ_STANDALONE=1 PX4_SYS_AUTOSTART=4001 PX4_GZ_MODEL_POSE="0,${Y_OFFSET},0.2,0,0,0" PX4_SIM_MODEL=gz_x500 ./build/px4_sitl_default/bin/px4 -d -i $i > /tmp/px4_${i}.log 2>&1 &
    
    # Wait 2 seconds before spawning next to prevent racing conditions
    sleep 2
  fi
done

echo "========================================="
echo "Fleet launch complete. Active instances:"
pgrep -f px4 || echo "No active px4 instances found."
echo "========================================="

# Keep script running to prevent the sandboxed terminal from cleaning up background processes
echo "Simulation running. Blocked on background processes. Stop/Kill task to exit."
wait
