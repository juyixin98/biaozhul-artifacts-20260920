"""Synthetic robot process used for end-to-end / acceptance verification.

The node is created with an explicit ROS namespace (e.g. ``/team/alpha``) and
subscribes to one or more latched topics inside it, printing every received
JSON command envelope. Two of these with *different* namespaces but the same
relative topic name demonstrate the same-name-topic isolation: the envelope
arrives only at the addressed robot.

Usage::

    mr-synthetic-robot alpha --namespace team/alpha --topics cmd/move,cmd/stop
    mr-synthetic-robot beta  --namespace team/beta  --topics cmd/move
"""

from __future__ import annotations

import argparse
import json
import sys

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
from std_msgs.msg import String

ROBOT_QOS = QoSProfile(
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
    history=HistoryPolicy.KEEP_LAST,
    depth=20,
)


class SyntheticRobot(Node):
    def __init__(self, name: str, namespace: str, topics: list[str]) -> None:
        super().__init__(name, namespace=namespace)
        self._count = 0
        self._topics = topics
        for topic in topics:
            if topic.startswith("/"):
                raise ValueError(
                    "synthetic robot only takes relative topics; got " + topic)
            self.create_subscription(
                String, "/" + namespace.strip("/") + "/" + topic.strip("/"),
                self._make_cb(topic), ROBOT_QOS)
        self.get_logger().info(
            f"synthetic robot {name!r} up in namespace /{namespace.strip('/')} "
            f"listening on {topics}")

    def _make_cb(self, topic: str):
        def cb(msg: String) -> None:
            self._count += 1
            try:
                envelope = json.loads(msg.data)
                rid = envelope.get("rid")
                ns = envelope.get("ns")
                seq = envelope.get("seq")
                tester = envelope.get("tester_id")
                payload = envelope.get("payload")
                print(
                    f"[RECV] robot={self.get_name()} ns={self.get_namespace()} "
                    f"topic={topic} from_rid={rid} from_ns={ns} seq={seq} "
                    f"tester={tester} payload={json.dumps(payload, ensure_ascii=False)}",
                    flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[RECV-RAW] {self.get_name()} {topic}: {msg.data!r} "
                      f"(parse error: {exc})", flush=True)
        return cb


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Synthetic ROS 2 robot")
    p.add_argument("name", help="node name, e.g. alpha")
    p.add_argument("--namespace", required=True,
                   help="relative namespace body, e.g. team/alpha")
    p.add_argument("--topics", default="cmd/move",
                   help="comma-separated relative topics")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.namespace.startswith("/"):
        print("namespace must be relative (no leading /)", file=sys.stderr)
        raise SystemExit(2)
    topics = [t.strip() for t in args.topics.split(",") if t.strip()]
    rclpy.init()
    node = SyntheticRobot(args.name, args.namespace, topics)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
