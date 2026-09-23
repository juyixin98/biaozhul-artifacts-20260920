"""Output sinks for replayed messages.

Two sinks are provided and both can be active at once:

* :class:`RingSink` keeps the last N published records in memory. It makes the
  service testable and observable without any DDS timing and backs the
  ``/sessions/{id}/published`` endpoint.
* :class:`RosSink` republishes raw CDR bytes over DDS using a real rclpy node.
  Publishers are created lazily per topic with the topic's original message
  type (resolved dynamically), so no schema code generation is needed.

Sinks receive *scheduled* records already stamped with the **original bag
timestamp**. The speed setting only changes wall-clock spacing between
messages; it never rewrites the carried timestamp. That invariant is enforced
here and documented on the record.
"""
from __future__ import annotations

import importlib
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class PublishedRecord:
    generation: int
    seq: int
    topic: str
    msg_type: str
    bag_timestamp_ns: int  # original, unmodified by rate changes
    wall_published_ns: int
    data_len: int


class Sink(Protocol):
    def emit(self, record: PublishedRecord, data: bytes) -> None: ...

    def close(self) -> None: ...


class RingSink:
    """Bounded, thread-safe record of what was published (payload excluded)."""

    def __init__(self, capacity: int = 2048) -> None:
        self._lock = threading.Lock()
        self._items: deque[PublishedRecord] = deque(maxlen=capacity)

    def emit(self, record: PublishedRecord, data: bytes) -> None:
        with self._lock:
            self._items.append(record)

    def snapshot(
        self,
        generation: int | None = None,
        since_seq: int | None = None,
    ) -> list[PublishedRecord]:
        with self._lock:
            out = list(self._items)
        if generation is not None:
            out = [r for r in out if r.generation == generation]
        if since_seq is not None:
            out = [r for r in out if r.seq > since_seq]
        return out

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def close(self) -> None:  # pragma: no cover - symmetry
        pass


def _load_message_class(type_str: str) -> Any:
    """Resolve ``package/msg/Name`` to its rclpy message class."""
    parts = type_str.split("/")
    if len(parts) != 3 or parts[1] != "msg":
        raise ValueError(f"unsupported message type: {type_str!r}")
    package, name = parts[0], parts[2]
    module = importlib.import_module(f"{package}.msg")
    return getattr(module, name)


class RosSink:
    """Publish raw CDR over DDS on ``<prefix><topic>``.

    rclpy publishers accept ``bytes`` (a serialized CDR payload) directly, so
    we create one typed publisher per topic using the bag's stored type and
    forward the untouched bytes.
    """

    def __init__(
        self,
        node: Any,
        topic_types: dict[str, str],
        prefix: str = "/replay",
        qos_depth: int = 100,
    ) -> None:
        self._node = node
        self._prefix = prefix or ""
        self._topic_types = topic_types
        self._publishers: dict[str, Any] = {}
        self._qos_depth = qos_depth
        self._lock = threading.Lock()

    def _target_topic(self, topic: str) -> str:
        if not self._prefix:
            return topic
        if self._prefix.endswith("/"):
            return self._prefix + topic.lstrip("/")
        return self._prefix + topic

    def _publisher_for(self, topic: str) -> Any:
        with self._lock:
            pub = self._publishers.get(topic)
            if pub is not None:
                return pub
            type_str = self._topic_types.get(topic)
            if not type_str:
                raise KeyError(f"no type known for topic {topic!r}")
            msg_class = _load_message_class(type_str)
            pub = self._node.create_publisher(
                msg_class, self._target_topic(topic), self._qos_depth
            )
            self._publishers[topic] = pub
            return pub

    def emit(self, record: PublishedRecord, data: bytes) -> None:
        pub = self._publisher_for(record.topic)
        # Raw serialized CDR is forwarded unchanged; rclpy handles it directly.
        pub.publish(data)

    def target_topics(self) -> dict[str, str]:
        """Map source topic -> published DDS topic (for verifier nodes)."""
        return {t: self._target_topic(t) for t in self._topic_types}

    def close(self) -> None:
        with self._lock:
            for pub in self._publishers.values():
                try:
                    self._node.destroy_publisher(pub)
                except Exception:
                    pass
            self._publishers.clear()


class MultiSink:
    """Fan a record out to several sinks; a failing sink never stops playback."""

    def __init__(self, sinks: list[Sink]) -> None:
        self._sinks = sinks

    def emit(self, record: PublishedRecord, data: bytes) -> None:
        for sink in self._sinks:
            try:
                sink.emit(record, data)
            except Exception:  # pragma: no cover - defensive isolation
                continue

    def close(self) -> None:
        for sink in self._sinks:
            try:
                sink.close()
            except Exception:
                pass
