"""rclpy monitor node: polls the real ROS graph and ingests beacons.

Runs in its own thread (rclpy executor) so FastAPI can live in the main
thread. Every poll:
  1. enumerates topic names+types from the DDS graph (get_topic_names_and_types)
  2. asks rmw for publisher/subscription endpoint info per topic
     (get_publishers_info_by_topic / get_subscriptions_info_by_topic) —
     these carry GIDs and the QoS policies DDS actually shares
  3. hands endpoints to TopologyStore; beacons are fused as they arrive
  4. advances the absent/grace lifecycle
"""
from __future__ import annotations

import threading
import time
from typing import Any

from rclpy.node import Node
from rclpy.qos import QoSProfile
from std_msgs.msg import String

from ..protocol import ANNOUNCE_TOPIC, parse_beacon
from ..qos_model import durability_name, history_name, reliability_name
from ..topology import Announcement, TopologyStore
from . import announce_qos_profile


class MonitorNode(Node):
    def __init__(self, store: TopologyStore, poll_period_s: float = 0.5,
                 grace_period_s: float = 10.0):
        super().__init__("qos_diag_monitor")
        self.store = store
        self.poll_period_s = poll_period_s
        self.grace_period_s = grace_period_s
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self.create_subscription(String, ANNOUNCE_TOPIC, self._on_beacon,
                                 announce_qos_profile())
        self._tick_count = 0

    def _on_beacon(self, msg: String):
        data = parse_beacon(msg.data)
        if data is None:
            return
        q = data["qos"]
        ann = Announcement(
            node_name=data["node_name"], node_namespace=data.get("node_namespace", "/"),
            role=data["role"], topic=data["topic"], gid=data.get("gid"),
            message_type=data.get("message_type"),
            reliability=q["reliability"], durability=q["durability"],
            history=q["history"], depth=int(q["depth"]),
            seq=int(data.get("seq", 0)), sent_ts=float(data.get("sent_ts", time.time())),
        )
        with self._lock:
            self.store.apply_announcement(ann, time.time())

    def poll_once(self):
        now = time.time()
        present: set[str] = set()
        try:
            topics = self.get_topic_names_and_types(no_demangle=False)
        except Exception:
            topics = []
        for topic, type_list in topics:
            if topic == ANNOUNCE_TOPIC:
                continue
            mtype = type_list[0] if type_list else None
            self._collect(topic, mtype, "publisher",
                          self.get_publishers_info_by_topic(topic, no_mangle=False),
                          present, now)
            self._collect(topic, mtype, "subscription",
                          self.get_subscriptions_info_by_topic(topic, no_mangle=False),
                          present, now)
        with self._lock:
            self.store.mark_absent(present, now)
        self._tick_count += 1

    def _collect(self, topic: str, mtype: str | None, role: str,
                 infos: list[Any], present: set[str], now: float):
        for info in infos:
            q = info.qos_profile
            gid = bytes(info.endpoint_gid).hex()
            with self._lock:
                ep = self.store.upsert_discovered(
                    gid=gid, topic=topic, role=role,
                    node=info.node_name, ns=info.node_namespace,
                    message_type=mtype,
                    rmw_reliability=reliability_name(q.reliability),
                    rmw_durability=durability_name(q.durability),
                    rmw_history=history_name(q.history),
                    rmw_depth=int(getattr(q, "depth", 0) or 0),
                    ts=now,
                )
            present.add(TopologyStore.key(gid, topic, role,
                                          info.node_name, info.node_namespace))

    def run(self):
        self.get_logger().info(
            f"qos_diag monitor started (poll={self.poll_period_s}s, "
            f"grace={self.grace_period_s}s)")
        try:
            while not self._stop.is_set():
                # spin_once services callbacks (beacons) AND we poll the graph
                t_end = time.time() + self.poll_period_s
                self.poll_once()
                # drain pending beacon callbacks for the remainder of the period
                while time.time() < t_end and not self._stop.is_set():
                    import rclpy
                    rclpy.spin_once(self, timeout_sec=min(0.1, self.poll_period_s))
        finally:
            self.get_logger().info("qos_diag monitor stopping")

    def stop(self):
        self._stop.set()
