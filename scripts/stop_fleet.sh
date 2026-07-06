#!/usr/bin/env bash

# Stop all running instances of PX4 and Gazebo Sim.

echo "========================================="
echo "Stopping PX4 SITL and Gazebo Fleet..."
echo "========================================="

# Forcefully kill any running px4 instances
pkill -9 -f px4 || true

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
