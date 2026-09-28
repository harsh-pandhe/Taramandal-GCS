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
# in backend/drone_manager.py so the GCS fleet-frame math lines up with spawns.
PAD_SPACING_Y=${PAD_SPACING_Y:-2}

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
  # Disable trap to avoid recursion on exit
  trap - SIGINT SIGTERM EXIT
  echo ""
  echo "========================================="
  echo "Stopping fleet and cleaning up background processes..."
  bash "$SCRIPT_DIR/stop_fleet.sh"
  echo "========================================="
  exit 0
}
trap cleanup SIGINT SIGTERM EXIT

# Source Gazebo environment
echo "Sourcing Gazebo Sim environment..."
source "$PX4_AUTOPILOT_DIR/build/px4_sitl_default/rootfs/gz_env.sh"

# Export correct world name
export PX4_GZ_WORLD=drone_show_field

# Isolate Gazebo partition to avoid conflicts with other simulations
export GZ_PARTITION=taramandal

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
  # fleet-frame pad offset — see PAD_SPACING_Y above).
  Y_OFFSET=$((PAD_SPACING_Y * i))
  
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
