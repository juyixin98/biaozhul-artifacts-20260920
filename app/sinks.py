"""Publish sinks used by the replay engine.

Two transports:

* ``loopback`` (default, no DDS): published envelopes are fanned out to
  in-process subscribers (the NDJSON stream endpoint) and kept in a bounded
  history. This is what makes the service fully testable on a headless box.
* ``ros``: the recorded CDR payloads are republished on their original topics
  using rclpy raw publishers, and control markers are published as
  ``std_msgs/msg/String`` JSON on the control topic.

A sink MUST return quickly from ``publish_*``; the engine calls them while
holding its generation-checked lock, which is what makes the guarantee
"queued messages from a superseded generation are never published" exact.
"""
from __future__ import annotations

import collections
import json
import queue
import threading
from typing import Any, Protocol, runtime_checkable

HISTORY_MAX = 20000
SUBSCRIBER_QUEUE_MAX = 4096


@runtime_checkable
class Sink(Protocol):
    name: str

    def publish_message(self, envelope: dict[str, Any]) -> None: ...
    def publish_event(self, event: dict[str, Any]) -> None: ...
    def close(self) -> None: ...


class LoopbackSink:
    """In-process fan-out sink with bounded history and per-client queues."""

    name = "loopback"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._history: collections.deque[dict[str, Any]] = collections.deque(
            maxlen=HISTORY_MAX
        )
        self._subscribers: dict[int, queue.Queue[dict[str, Any] | None]] = {}
        self._dropped: dict[int, int] = {}
        self._next_id = 0
        self._next_history_id = 0

    # -- engine facing --------------------------------------------------------

    def publish_message(self, envelope: dict[str, Any]) -> None:
        with self._lock:
            self._next_history_id += 1
            item = dict(envelope)
            item["history_id"] = self._next_history_id
            self._history.append(item)
            dead: list[int] = []
            for cid, q in self._subscribers.items():
                try:
                    q.put_nowait(item)
                except queue.Full:
                    self._dropped[cid] = self._dropped.get(cid, 0) + 1
                    if self._dropped[cid] > SUBSCRIBER_QUEUE_MAX:
                        dead.append(cid)
            for cid in dead:
                q = self._subscribers.pop(cid, None)
                if q is not None:
                    q.put_nowait({"kind": "slow_consumer", "dropped": self._dropped[cid]})
            result = None
        # pure in-memory work; nothing to await
        return result

    def publish_event(self, event: dict[str, Any]) -> None:
        self.publish_message(event)

    # -- API facing -----------------------------------------------------------

    def subscribe(self) -> tuple[int, queue.Queue[dict[str, Any] | None]]:
        q: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        with self._lock:
            self._next_id += 1
            cid = self._next_id
            self._subscribers[cid] = q
        return cid, q

    def unsubscribe(self, cid: int) -> None:
        with self._lock:
            q = self._subscribers.pop(cid, None)
        if q is not None:
            q.put(None)

    def history(
        self, after_history_id: int = 0, limit: int = 1000
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = [
                dict(item)
                for item in self._history
                if item["history_id"] > after_history_id
            ]
        return items[:limit]

    def history_tail_id(self) -> int:
        with self._lock:
            return self._next_history_id

    def close(self) -> None:
        with self._lock:
            subs = list(self._subscribers.items())
            self._subscribers.clear()
        for _, q in subs:
            q.put(None)


# ---------------------------------------------------------------------------
# ROS / DDS sink
# ---------------------------------------------------------------------------


class RosSink:
    """Republish recorded CDR bytes on their original topics via rclpy.

    rclpy is imported lazily so the rest of the package works without a
    sourced ROS environment (only this sink needs it).
    """

    name = "ros"

    def __init__(self, session_id: str, control_topic: str) -> None:
        import rclpy  # noqa: F401  (presence check)
        from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy

        self._session_id = session_id
        self._control_topic = control_topic
        self._qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_ALL,
        )
        self._node: Any = None
        self._spin_thread: threading.Thread | None = None
        self._publishers: dict[tuple[str, str], Any] = {}
        self._string_cls: Any = None

    def _ensure_node(self) -> None:
        if self._node is not None:
            return
        import rclpy
        from rclpy.node import Node
        from std_msgs.msg import String

        if not rclpy.ok():
            rclpy.init()
        self._rclpy = rclpy
        self._node = Node(f"replay_sink_{self._session_id.replace('-', '_')[:16]}")
        self._string_cls = String
        self._publishers[(self._control_topic, "std_msgs/msg/String")] = (
            self._node.create_publisher(String, self._control_topic, self._qos)
        )

        def spin() -> None:
            try:
                while self._node is not None and rclpy.ok():
                    rclpy.spin_once(self._node, timeout_sec=0.1)
            except Exception:
                pass

        self._spin_thread = threading.Thread(
            target=spin, name=f"ros-spin-{self._session_id[:8]}", daemon=True
        )
        self._spin_thread.start()

    def _publisher_for(self, topic: str, topic_type: str) -> Any:
        key = (topic, topic_type)
        pub = self._publishers.get(key)
        if pub is None:
            cls = _message_class(topic_type)
            pub = self._node.create_publisher(cls, topic, self._qos)
            self._publishers[key] = pub
        return pub

    def publish_message(self, envelope: dict[str, Any]) -> None:
        import base64

        self._ensure_node()
        pub = self._publisher_for(envelope["topic"], envelope["type"])
        pub.publish(base64.b64decode(envelope["data_b64"]))

    def publish_event(self, event: dict[str, Any]) -> None:
        self._ensure_node()
        pub = self._publishers[(self._control_topic, "std_msgs/msg/String")]
        marker = self._string_cls(data=json.dumps(event, separators=(",", ":")))
        pub.publish(marker)

    def close(self) -> None:
        node = self._node
        self._node = None
        if node is not None:
            import rclpy

            try:
                node.destroy_node()
            except Exception:
                pass
            # Other sessions may still hold the global rclpy context; shutdown
            # is handled by process teardown.
            _ = rclpy


def _message_class(topic_type: str) -> Any:
    """Import a ROS message class from its ``pkg/msg/Name`` identifier."""
    import importlib

    parts = topic_type.split("/")
    if len(parts) != 3 or parts[1] != "msg":
        raise ValueError(f"unsupported topic type: {topic_type!r}")
    package, _, name = parts
    module = importlib.import_module(f"{package}.msg")
    try:
        return getattr(module, name)
    except AttributeError as exc:
        raise ValueError(f"unknown message type {topic_type}") from exc
