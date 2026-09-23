"""Opt-in end-to-end DDS test: real rclpy publisher and subscriber.

Skipped unless ``RUN_DDS_TESTS=1`` and a sourced ROS environment provides
``rclpy`` / ``rosbag2_py``. This proves the RosSink actually republishes raw
CDR on DDS topics and that a live subscriber receives ordered messages.
"""
from __future__ import annotations

import os
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DDS_TESTS") != "1",
    reason="set RUN_DDS_TESTS=1 (and source ROS) to run DDS tests",
)

rclpy = pytest.importorskip("rclpy")
rosbag2_py = pytest.importorskip("rosbag2_py")

from example_interfaces.msg import Int64  # noqa: E402

from rosreplay.config import Settings  # noqa: E402
from rosreplay.sessions import SessionManager  # noqa: E402


def test_live_subscriber_receives_replay(tmp_path, tmp_bag_root, helpers):
    settings = Settings(
        bag_root=tmp_bag_root,
        checkpoint_dir=tmp_path / "cp",
        ros_enabled=True,
        topic_prefix="/replay",
    )
    manager = SessionManager(settings)

    # Let the service's ros_runtime own rclpy init; build the subscriber node
    # through it so both share the single allowed context.
    from rosreplay import ros_runtime

    node = ros_runtime.init_node("dds_test_subscriber")
    import threading

    lock = threading.Lock()
    received: list[tuple[str, int]] = []

    def make_cb(topic):
        def cb(msg):
            with lock:
                received.append((topic, msg.data))
        return cb

    node.create_subscription(Int64, "/replay/tick", make_cb("/tick"), 100)
    node.create_subscription(Int64, "/replay/tock", make_cb("/tock"), 100)

    # Start paused; the act of creating the session builds the RosSink, and we
    # force publisher creation before play so DDS discovery can complete.
    session = manager.create_session(
        "demo", topics=["/tick", "/tock"], rate=1.0, auto_play=False
    )
    assert session.ros is not None
    for topic in ("/tick", "/tock"):
        session.ros._publisher_for(topic)

    # Wait until the subscriber actually discovers both publishers.
    discover_deadline = time.monotonic() + 8
    while time.monotonic() < discover_deadline:
        rclpy.spin_once(node, timeout_sec=0.05)
        if (
            node.count_publishers("/replay/tick") >= 1
            and node.count_publishers("/replay/tock") >= 1
        ):
            break
    assert node.count_publishers("/replay/tick") >= 1
    assert node.count_publishers("/replay/tock") >= 1
    time.sleep(0.5)  # let QOS endpoints fully match before the first message

    session.engine.resume()  # real-time (1x) replay of the ~1s bag
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=0.05)
        with lock:
            n = len(received)
        if session.engine.status()["state"] == "finished" and n >= 22:
            time.sleep(0.3)  # drain final in-flight messages
            break
    for _ in range(10):
        rclpy.spin_once(node, timeout_sec=0.05)

    # ros_runtime owns the shared context; shutdown_all tears publishers down.
    manager.shutdown_all()

    ticks = [v for t, v in received if t == "/tick"]
    assert ticks == list(range(11)), ticks
    tocks = [v for t, v in received if t == "/tock"]
    assert tocks == list(range(11)), tocks
