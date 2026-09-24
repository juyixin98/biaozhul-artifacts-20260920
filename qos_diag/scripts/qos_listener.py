#!/usr/bin/env python3
"""真实 ROS 2 订阅节点，QoS 四元组可配置，持续输出“实际收消息数”到状态文件。

接收计数是与规则引擎相互独立的物理证据：
* 规则判 INCOMPATIBLE 时，真实 DDS 里 count 必须保持 0；
* 配置修正后 count 必须开始增长 —— 用它证明诊断与恢复是真实验证过的。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String

REL = {"reliable": ReliabilityPolicy.RELIABLE, "best_effort": ReliabilityPolicy.BEST_EFFORT}
DUR = {"volatile": DurabilityPolicy.VOLATILE, "transient_local": DurabilityPolicy.TRANSIENT_LOCAL}
HIST = {"keep_last": HistoryPolicy.KEEP_LAST, "keep_all": HistoryPolicy.KEEP_ALL}


class QoSListener(Node):
    def __init__(self, topic, qos, args):
        super().__init__("qos_listener")
        self.args = args
        self.count = 0
        self.last_data = ""
        self.started = time.time()
        self.sub = self.create_subscription(String, topic, self._cb, qos)
        self.get_logger().info(
            f"listener ready topic={topic} reliability={args.reliability} "
            f"durability={args.durability} history={args.history} depth={args.depth}"
        )

    def _cb(self, msg: String) -> None:
        self.count += 1
        self.last_data = msg.data
        self.get_logger().info(f"received #{self.count}: {msg.data}", throttle_duration_sec=1.0)
        self._write_status()

    def _write_status(self) -> None:
        data = {
            "received": self.count,
            "last_data": self.last_data,
            "uptime": time.time() - self.started,
            "topic": self.args.topic,
            "requested_qos": {
                "reliability": self.args.reliability,
                "durability": self.args.durability,
                "history": self.args.history,
                "depth": self.args.depth,
            },
        }
        tmp = self.args.status_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, self.args.status_file)


def parse_args(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--topic", default="/qos_demo/chatter")
    p.add_argument("--reliability", choices=list(REL), default="reliable")
    p.add_argument("--durability", choices=list(DUR), default="volatile")
    p.add_argument("--history", choices=list(HIST), default="keep_last")
    p.add_argument("--depth", type=int, default=10)
    p.add_argument("--status-file", default="listener_status.json")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    qos_kwargs = dict(reliability=REL[args.reliability], durability=DUR[args.durability],
                      history=HIST[args.history])
    if args.history == "keep_last":
        qos_kwargs["depth"] = args.depth
    qos = QoSProfile(**qos_kwargs)

    rclpy.init()
    node = QoSListener(args.topic, qos, args)
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
