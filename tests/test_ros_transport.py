"""Real DDS integration test for transport=ros.

Publishes the recorded CDR bytes on the bag's original topics via rclpy raw
publishers and verifies delivery with a raw subscriber. This is the only
test that exercises DDS discovery, so it tolerates a slightly longer timeout
and runs serially (a single rclpy context per process).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from tests.conftest import requires_ros


@requires_ros
def test_ros_transport_republishes_cdr_on_topics(good_bag):
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
    from std_msgs.msg import String

    from app import bagstore
    from app.playback import ReplayEngine
    from app.sinks import RosSink

    bag_dir = Path(good_bag["uri"])
    index, payloads = bagstore.index_bag_with_payloads(bag_dir)

    rclpy.init()
    node = Node("ros_integration_subscriber")
    qos = QoSProfile(
        reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_ALL
    )
    received: list[tuple[str, bytes]] = []
    lock = threading.Lock()

    for topic in sorted({e.topic for e in index.entries}):
        node.create_subscription(
            String,
            topic,
            (lambda t: lambda raw: received.append((t, bytes(raw))))(topic),
            qos,
            raw=True,
        )

    control_events: list[dict] = []

    def control_cb(msg: String) -> None:
        import json

        try:
            control_events.append(json.loads(msg.data))
        except Exception:
            pass

    node.create_subscription(String, "/replay/control", control_cb, qos)

    sink = RosSink("integ0001", "/replay/control")
    engine = ReplayEngine(
        index,
        sink,
        tick_seconds=0.002,
        payloads=payloads,
        session_id="integ0001",
    )

    def spin() -> None:
        while rclpy.ok() and spinning[0]:
            rclpy.spin_once(node, timeout_sec=0.05)

    spinning = [True]
    spin_thread = threading.Thread(target=spin, daemon=True)
    spin_thread.start()

    try:
        engine.set_rate(10.0)
        engine.play()
        # Wait for DDS discovery between sink publishers and subscriber.
        deadline = time.time() + 10
        while time.time() < deadline and len(received) < len(index.entries):
            time.sleep(0.1)
        assert len(received) == len(index.entries), (
            f"expected {len(index.entries)} got {len(received)}"
        )

        # Every original payload was delivered on its original topic.
        expected = {
            (entry.topic, entry.data_sha256) for entry in index.entries
        }
        import hashlib

        got = {(t, hashlib.sha256(data).hexdigest()) for t, data in received}
        assert got == expected

        # Now a seek opens a new generation and republishes only the tail;
        # the control topic must carry the seek marker.
        before = len(received)
        marker_gens_before = {e.get("generation") for e in control_events}
        engine.seek(seq=len(index.entries) - 2, play_after=True)
        deadline = time.time() + 10
        while time.time() < deadline and len(received) < before + 3:
            time.sleep(0.1)
        assert len(received) >= before + 3
        assert any(
            e.get("kind") == "seek" and e.get("generation") not in marker_gens_before
            for e in control_events
        )
    finally:
        spinning[0] = False
        engine.shutdown()
        spin_thread.join(timeout=2)
        node.destroy_node()
        rclpy.shutdown()
