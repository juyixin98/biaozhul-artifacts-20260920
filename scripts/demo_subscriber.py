#!/usr/bin/env python3
"""Real instrumented ROS 2 subscriber used to stage QoS scenarios."""
from __future__ import annotations

import argparse
import signal
import sys
import time
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
    p.add_argument("--name", default="demo_sub")
    p.add_argument("--topic", default="/qos_demo/data")
    p.add_argument("--reliability", choices=list(REL), default="reliable")
    p.add_argument("--durability", choices=list(DUR), default="volatile")
    p.add_argument("--history", choices=list(HIST), default="keep_last")
    p.add_argument("--depth", type=int, default=10)
    p.add_argument("--runtime", type=float, default=0)
    return p.parse_args()


def main():
    args = parse_args()
    rclpy.init()
    qos = QoSProfile(reliability=REL[args.reliability],
                     durability=DUR[args.durability],
                     history=HIST[args.history], depth=args.depth)
    node = DiagnosticsNode(args.name)
    received = {"n": 0}

    def cb(msg):
        received["n"] += 1

    node.create_subscription(String, args.topic, cb, qos)
    node.start_announcing(1.0)

    stop = {"v": False}

    def _sig(s, f):
        stop["v"] = True
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    node.get_logger().info(
        f"subscribing {args.topic} rel={args.reliability} dur={args.durability} "
        f"hist={args.history} depth={args.depth}")
    t_end = time.time() + args.runtime if args.runtime > 0 else None
    last_report = time.time()
    try:
        while not stop["v"]:
            rclpy.spin_once(node, timeout_sec=0.1)
            if time.time() - last_report >= 5:
                node.get_logger().info(f"received {received['n']} messages")
                last_report = time.time()
            if t_end and time.time() >= t_end:
                break
    finally:
        node.destroy_node()
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
