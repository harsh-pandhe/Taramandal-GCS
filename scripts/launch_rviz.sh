#!/usr/bin/env bash
# Bridges the running Gazebo drone-show simulation's pose topic into ROS 2 TF
# and opens RViz to visualize the fleet. Requires:
#   - scripts/launch_fleet.sh already running (same GZ_PARTITION)
#   - ROS 2 Jazzy with ros_gz_bridge + rviz2 installed
#   - a display (this opens a GUI window; will not work in a headless-only session)
#
# Usage: ./scripts/launch_rviz.sh [-w <world_name>] [-p <gz_partition>]

set -e

WORLD="drone_show_field"
PARTITION="taramandal"

while getopts "w:p:" opt; do
  case $opt in
    w) WORLD=$OPTARG ;;
    p) PARTITION=$OPTARG ;;
    *) echo "Usage: $0 [-w <world_name>] [-p <gz_partition>]"; exit 1 ;;
  esac
done

if [ -z "$ROS_DISTRO" ]; then
  echo "Sourcing ROS 2 Jazzy..."
  # shellcheck disable=SC1091
  source /opt/ros/jazzy/setup.bash
fi

if [ -z "$DISPLAY" ]; then
  echo "Error: no DISPLAY set. RViz needs a GUI display; this won't work in a headless-only session."
  exit 1
fi

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

cleanup() {
  trap - SIGINT SIGTERM EXIT
  echo ""
  echo "Stopping ros_gz_bridge..."
  [ -n "$BRIDGE_PID" ] && kill "$BRIDGE_PID" 2>/dev/null
  exit 0
}
trap cleanup SIGINT SIGTERM EXIT

echo "========================================="
echo "Bridging Gazebo pose -> ROS 2 TF"
echo "  World:     $WORLD"
echo "  Partition: $PARTITION"
echo "========================================="

# NOTE: `ros_gz_bridge parameter_bridge <topic>@tf2_msgs/msg/TFMessage[gz.msgs.Pose_V`
# (the "obvious" bridge invocation) does not populate frame_id/child_frame_id on
# ros-gz-bridge 1.0.22 / ROS 2 Jazzy — RViz would receive anonymous, unusable
# transforms. gz_tf_bridge.py reads the raw Gazebo Pose_V message (which carries
# each drone's name, e.g. "x500_0") directly and republishes correctly-named /tf.
GZ_PARTITION="$PARTITION" python3 "$SCRIPT_DIR/gz_tf_bridge.py" --world "$WORLD" &
BRIDGE_PID=$!

echo "Waiting 3 seconds for the bridge to come up..."
sleep 3

echo "Launching RViz2..."
rviz2 -d "$SCRIPT_DIR/../rviz/taramandal.rviz"

cleanup
