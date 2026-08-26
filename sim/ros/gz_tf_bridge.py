#!/usr/bin/env python3
"""
Bridges Gazebo's per-model pose stream into a proper, named ROS 2 /tf so RViz's
TF display can show each drone as a distinct, labeled frame.

Why not `ros_gz_bridge parameter_bridge <topic>@tf2_msgs/msg/TFMessage[gz.msgs.Pose_V`?
That auto-conversion (ros-gz-bridge 1.0.22, ROS 2 Jazzy) does not populate
`frame_id`/`child_frame_id` on the resulting TransformStamped messages, so RViz
receives anonymous, unusable transforms. This node reads the raw Gazebo
Pose_V message directly (which carries each entity's name, e.g. "x500_0") via
gz-transport's Python bindings, and republishes correctly-named transforms.

Usage: GZ_PARTITION=taramandal python3 scripts/gz_tf_bridge.py \
           --world drone_show_field
"""

import argparse
import sys

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import TransformStamped
from tf2_ros import TransformBroadcaster

from gz.transport13 import Node as GzNode
from gz.msgs10.pose_v_pb2 import Pose_V


class GzTfBridge(Node):
    def __init__(self, world: str):
        super().__init__("gz_tf_bridge")
        self.world_frame = world
        self.broadcaster = TransformBroadcaster(self)

        self.gz_node = GzNode()
        topic = f"/world/{world}/dynamic_pose/info"
        ok = self.gz_node.subscribe(Pose_V, topic, self._on_pose_v)
        if not ok:
            self.get_logger().error(f"Failed to subscribe to Gazebo topic: {topic}")
            sys.exit(1)
        self.get_logger().info(f"Subscribed to {topic}, publishing named /tf frames.")

    def _on_pose_v(self, msg: Pose_V):
        now = self.get_clock().now().to_msg()
        for pose in msg.pose:
            if not pose.name:
                continue
            t = TransformStamped()
            t.header.stamp = now
            t.header.frame_id = self.world_frame
            t.child_frame_id = pose.name
            t.transform.translation.x = pose.position.x
            t.transform.translation.y = pose.position.y
            t.transform.translation.z = pose.position.z
            t.transform.rotation.x = pose.orientation.x
            t.transform.rotation.y = pose.orientation.y
            t.transform.rotation.z = pose.orientation.z
            t.transform.rotation.w = pose.orientation.w
            self.broadcaster.sendTransform(t)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--world", default="drone_show_field")
    args = parser.parse_args()

    rclpy.init()
    node = GzTfBridge(args.world)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
