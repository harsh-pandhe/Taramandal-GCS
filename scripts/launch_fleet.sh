#!/usr/bin/env bash

# Exit immediately if a command exits with a non-zero status
set -e

# Default parameters
NUM_DRONES=3
HEADLESS=0

# Parse options
while getopts "n:h" opt; do
  case $opt in
    n)
      NUM_DRONES=$OPTARG
      ;;
    h)
      HEADLESS=1
      ;;
    *)
      echo "Usage: $0 [-n <num_drones>] [-h]"
      exit 1
      ;;
  esac
done

if [ "$NUM_DRONES" -lt 1 ] || [ "$NUM_DRONES" -gt 10 ]; then
  echo "Error: Number of drones must be between 1 and 10."
  exit 1
fi

echo "========================================="
echo "Starting multi-drone SITL simulation..."
echo "  Number of drones: $NUM_DRONES"
echo "  Headless mode: $HEADLESS"
echo "========================================="

# Call cleanup first
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
bash "$SCRIPT_DIR/stop_fleet.sh"

# Source Gazebo environment
echo "Sourcing Gazebo Sim environment..."
source /home/harsh-pandhe/PX4-Autopilot/build/px4_sitl_default/rootfs/gz_env.sh

# Export correct world name
export PX4_GZ_WORLD=drone_show_field

# Isolate Gazebo partition to avoid conflicts with other simulations
export GZ_PARTITION=taramandal

# Disable interactive PX4 shell to prevent EOF infinite loops in log files
export NO_PXH=1

# Export Headless if requested
if [ "$HEADLESS" -eq 1 ]; then
  export HEADLESS=1
fi

# Change directory to PX4 Autopilot root to resolve relative configuration paths
cd /home/harsh-pandhe/PX4-Autopilot

# Spawning instances
for ((i=0; i<NUM_DRONES; i++)); do
  # Calculate horizontal spacing offset: 2 meters apart on Y axis
  Y_OFFSET=$((2 * i))
  
  if [ "$i" -eq 0 ]; then
    echo "Launching host drone (instance 0) at Y_OFFSET = 0m..."
    PX4_SYS_AUTOSTART=4001 PX4_SIM_MODEL=gz_x500 ./build/px4_sitl_default/bin/px4 -d -i 0 > /tmp/px4_0.log 2>&1 &
    
    # Wait for gazebo server and host instance to settle down
    echo "Waiting 10 seconds for Gazebo Sim server to initialize..."
    sleep 10
  else
    echo "Launching follower drone (instance $i) at Y_OFFSET = ${Y_OFFSET}m..."
    PX4_GZ_STANDALONE=1 PX4_SYS_AUTOSTART=4001 PX4_GZ_MODEL_POSE="0,${Y_OFFSET},0,0,0,0" PX4_SIM_MODEL=gz_x500 ./build/px4_sitl_default/bin/px4 -d -i $i > /tmp/px4_${i}.log 2>&1 &
    
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
