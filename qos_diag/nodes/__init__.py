"""Helpers for instrumented demo nodes.

A DiagnosticsNode wraps a normal rclpy node and, in addition, republishes a
beacon every ``announce_period`` seconds for each endpoint it created. The
beacon is real ROS 2 traffic on /qos_diag/endpoints (latched) — the monitor
discovers it through DDS exactly like any other topic.
"""
from __future__ import annotations

import json
from typing import Any

from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from std_msgs.msg import String

from ..protocol import ANNOUNCE_TOPIC, build_beacon

_REL = {1: "RELIABLE", 2: "BEST_EFFORT", 0: "SYSTEM_DEFAULT", 3: "UNKNOWN"}
_DUR = {1: "TRANSIENT_LOCAL", 2: "VOLATILE", 0: "SYSTEM_DEFAULT", 3: "UNKNOWN"}
_HIST = {1: "KEEP_LAST", 2: "KEEP_ALL", 0: "SYSTEM_DEFAULT", 3: "UNKNOWN"}


def policy_names(qos: QoSProfile) -> dict[str, Any]:
    return {
        "reliability": _REL.get(int(qos.reliability), "UNKNOWN"),
        "durability": _DUR.get(int(qos.durability), "UNKNOWN"),
        "history": _HIST.get(int(qos.history), "UNKNOWN"),
        "depth": int(qos.depth),
    }


def announce_qos_profile() -> QoSProfile:
    return QoSProfile(
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.TRANSIENT_LOCAL,
        history=HistoryPolicy.KEEP_LAST,
        depth=100,
    )


class DiagnosticsNode(Node):
    """rclpy Node that tracks created endpoints and beacons their real QoS."""

    def __init__(self, name: str, **kwargs):
        # rclpy's Node.__init__ creates the internal rosout publisher via the
        # overridden create_publisher below, so the backing fields must exist
        # before super().__init__ runs.
        self._endpoints: list[dict[str, Any]] = []
        self._seq = 0
        super().__init__(name, **kwargs)
        self._announcer = self.create_publisher(String, ANNOUNCE_TOPIC,
                                                announce_qos_profile())

    def _record(self, role: str, topic: str, msg_type: Any, qos: QoSProfile):
        mod = getattr(msg_type, "__module__", "")
        cls = getattr(msg_type, "__name__", type(msg_type).__name__)
        # convert e.g. std_msgs.msg._string.String -> std_msgs/msg/String
        if ".msg." in mod:
            pkg = mod.split(".")[0]
            message_type = f"{pkg}/msg/{cls}"
        else:
            message_type = f"{mod}/{cls}"
        self._endpoints.append({
            "role": role, "topic": topic, "message_type": message_type,
            **policy_names(qos),
        })

    def create_publisher(self, msg_type, topic, qos, *args, **kwargs):
        pub = super().create_publisher(msg_type, topic, qos, *args, **kwargs)
        if topic != ANNOUNCE_TOPIC:
            self._record("publisher", topic, msg_type, qos)
        return pub

    def create_subscription(self, msg_type, topic, callback, qos, *args, **kwargs):
        sub = super().create_subscription(msg_type, topic, callback, qos,
                                          *args, **kwargs)
        self._record("subscription", topic, msg_type, qos)
        return sub

    def announce_once(self):
        for ep in self._endpoints:
            self._seq += 1
            raw = build_beacon(
                node_name=self.get_name(),
                node_namespace=self.get_namespace(),
                role=ep["role"], topic=ep["topic"],
                message_type=ep["message_type"],
                reliability=ep["reliability"], durability=ep["durability"],
                history=ep["history"], depth=ep["depth"], seq=self._seq)
            msg = String()
            msg.data = raw
            self._announcer.publish(msg)

    def start_announcing(self, period_s: float = 1.0):
        self._ann_timer = self.create_timer(period_s, self.announce_once)
        self.announce_once()
        return self._ann_timer
