"""Lazy rclpy lifecycle management.

rclpy is only initialised when a session actually wants DDS publishing, and it
is never imported when :envvar:`ROS_ENABLED` is false. That keeps the service
(and its test-suite) fully runnable on machines without a DDS backend.

The service node only *publishes*, so it does not need to be spun by an
executor. Deliberately not running a background spin thread avoids rclpy's
"executor is already spinning" error when an embedding application (or the
verifier node / DDS tests) spins the same context itself.
"""
from __future__ import annotations

import threading
from typing import Any

_init_lock = threading.Lock()
_initialized = False
_node: Any = None
_init_count = 0


def is_ros_available() -> bool:
    try:
        import rclpy  # noqa: F401
    except Exception:
        return False
    return True


def init_node(name: str = "ros_replay_service") -> Any:
    """Initialise rclpy once and return a shared node (no executor spun)."""
    global _initialized, _node
    import rclpy  # local import: optional dependency at runtime

    with _init_lock:
        if not _initialized:
            # Tolerate a context initialised elsewhere (e.g. an embedding test
            # or node): rclpy forbids a second Context.init in one process.
            try:
                rclpy.init()
            except RuntimeError as exc:
                if "only be called once" not in str(exc):
                    raise
            _initialized = True
        if _node is None:
            _node = rclpy.create_node(name)
    return _node


def shutdown() -> None:
    global _initialized, _node
    import rclpy

    with _init_lock:
        if _node is not None:
            try:
                _node.destroy_node()
            except Exception:
                pass
        if _initialized:
            try:
                rclpy.shutdown()
            except Exception:
                pass
        _initialized = False
        _node = None
