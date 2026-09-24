"""Entry points for ``ros2 run`` / direct execution."""

from __future__ import annotations

import rclpy

from .aligner_node import AlignerNode
from .synthetic_publisher import SyntheticPublisher


def main_aligner() -> None:
    rclpy.init()
    node = AlignerNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            node.shutdown()
        except Exception:  # noqa: BLE001 - best-effort drain on shutdown
            pass
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def main_publisher() -> None:
    rclpy.init()
    node = SyntheticPublisher()
    try:
        while rclpy.ok() and not node.done:
            rclpy.spin_once(node, timeout_sec=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        node.get_logger().info("publisher exit")
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
