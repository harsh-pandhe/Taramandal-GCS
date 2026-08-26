#!/usr/bin/env bash

# Stop all running instances of PX4 and Gazebo Sim.

echo "========================================="
echo "Stopping PX4 SITL and Gazebo Fleet..."
echo "========================================="

# Forcefully kill any running px4 SITL instances.
#
# This must NOT be a bare "px4" pattern: this script itself lives at
# sim/px4/stop_fleet.sh and is invoked as `bash .../sim/px4/launch_fleet.sh`,
# so `pkill -f px4` matches that command line too and kills its own caller
# before it finishes starting the fleet — self-inflicted, and only surfaced
# after the monorepo move put "px4" in the script's own path. Match the actual
# compiled binary invocation instead.
pkill -9 -f "px4_sitl_default/bin/px4" || true

# Forcefully kill any running MAVSDK servers
pkill -9 -f mavsdk_server || true

# Forcefully kill Gazebo Sim processes (specifically matching Gazebo command lines)
pkill -9 -f "gz sim" || true
pkill -9 -f "gz-sim-server" || true
pkill -9 -f "gz sim-server" || true
pkill -9 -f "ruby.*gz" || true

# Remove temporary lock or run directories if any
rm -rf ~/.gazebo/sys/lock* 2>/dev/null || true

echo "All simulation processes killed."
