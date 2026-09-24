#!/usr/bin/env python3
"""Real instrumented ROS 2 publisher used to stage QoS scenarios.

Examples:
  python3 -m scripts.demo_publisher --name pub_rel --topic /data \
      --reliability best_effort --durability volatile --history keep_last --depth 5

It publishes std_msgs/String at a fixed rate AND beacons its real configured
QoS on /qos_diag/endpoints.
"""
from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rclpy
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String

from qos_diag.nodes import DiagnosticsNode

REL = {"reliable": ReliabilityPolicy.RELIABLE,
       "best_effort": ReliabilityPolicy.BEST_EFFORT}
DUR = {"volatile": DurabilityPolicy.VOLATILE,
       "transient_local": DurabilityPolicy.TRANSIENT_LOCAL}
HIST = {"keep_last": HistoryPolicy.KEEP_LAST,
        "keep_all": HistoryPolicy.KEEP_ALL}


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--name", default="demo_pub")
    p.add_argument("--topic", default="/qos_demo/data")
    p.add_argument("--reliability", choices=list(REL), default="reliable")
    p.add_argument("--durability", choices=list(DUR), default="volatile")
    p.add_argument("--history", choices=list(HIST), default="keep_last")
    p.add_argument("--depth", type=int, default=10)
    p.add_argument("--rate", type=float, default=2.0)
    p.add_argument("--runtime", type=float, default=0,
                   help="seconds before auto-exit; 0 = until SIGTERM")
    return p.parse_args()


def main():
    args = parse_args()
    rclpy.init()
    qos = QoSProfile(reliability=REL[args.reliability],
                     durability=DUR[args.durability],
                     history=HIST[args.history], depth=args.depth)
    node = DiagnosticsNode(args.name)
    pub = node.create_publisher(String, args.topic, qos)
    node.start_announcing(1.0)

    stop = {"v": False}

    def _sig(s, f):
        stop["v"] = True
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    import time
    tick = 0
    t_end = time.time() + args.runtime if args.runtime > 0 else None
    last = 0.0
    node.get_logger().info(
        f"publishing {args.topic} rel={args.reliability} dur={args.durability} "
        f"hist={args.history} depth={args.depth}")
    try:
        while not stop["v"]:
            rclpy.spin_once(node, timeout_sec=0.05)
            now = time.time()
            if now - last >= 1.0 / args.rate:
                tick += 1
                msg = String()
                msg.data = f"seq={tick} t={now:.3f} qos={args.reliability}/{args.durability}"
                pub.publish(msg)
                last = now
            if t_end and time.time() >= t_end:
                break
    finally:
        node.destroy_node()
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
