#!/usr/bin/env python3
"""真实 ROS 2 发布节点，QoS 四元组全部可通过命令行配置。

用于制造/修复 QoS 不兼容场景，数据走真实 DDS（Fast DDS），不是静态 JSON。
"""

from __future__ import annotations

import argparse
import sys

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String

REL = {"reliable": ReliabilityPolicy.RELIABLE, "best_effort": ReliabilityPolicy.BEST_EFFORT}
DUR = {"volatile": DurabilityPolicy.VOLATILE, "transient_local": DurabilityPolicy.TRANSIENT_LOCAL}
HIST = {"keep_last": HistoryPolicy.KEEP_LAST, "keep_all": HistoryPolicy.KEEP_ALL}


class QoSTalker(Node):
    def __init__(self, topic: str, qos: QoSProfile, reliability: str, durability: str,
                 history: str, depth: int, rate: float):
        super().__init__("qos_talker")
        self.pub = self.create_publisher(String, topic, qos)
        self.count = 0
        self.timer = self.create_timer(rate, self._tick)
        self.get_logger().info(
            f"talker ready topic={topic} reliability={reliability} durability={durability} "
            f"history={history} depth={depth}",
            throttle_duration_sec=1.0,
        )

    def _tick(self) -> None:
        self.count += 1
        msg = String()
        msg.data = f"qos-demo #{self.count}"
        self.pub.publish(msg)


def parse_args(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--topic", default="/qos_demo/chatter")
    p.add_argument("--reliability", choices=list(REL), default="reliable")
    p.add_argument("--durability", choices=list(DUR), default="volatile")
    p.add_argument("--history", choices=list(HIST), default="keep_last")
    p.add_argument("--depth", type=int, default=10)
    p.add_argument("--rate", type=float, default=2.0)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    qos_kwargs = dict(reliability=REL[args.reliability], durability=DUR[args.durability],
                      history=HIST[args.history])
    if args.history == "keep_last":
        qos_kwargs["depth"] = args.depth
    qos = QoSProfile(**qos_kwargs)

    rclpy.init()
    node = QoSTalker(args.topic, qos, args.reliability, args.durability, args.history,
                     args.depth, args.rate)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
