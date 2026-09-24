"""Thread-safe in-memory event bus plus bounded per-robot audit history."""

from __future__ import annotations

import collections
import threading
import time
from collections import deque
from typing import Any, Deque

HISTORY_LIMIT = 100


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[collections.deque[dict[str, Any]]] = []
        self._history: dict[str, Deque[dict[str, Any]]] = {}
        self._status: dict[str, dict[str, Any]] = {}

    # -- internal -----------------------------------------------------------
    def _append_locked(self, robot_id: str, event: dict[str, Any]) -> None:
        hist = self._history.setdefault(robot_id, deque(maxlen=HISTORY_LIMIT))
        hist.append(event)
        for queue in self._subscribers:
            queue.append(event)

    # -- public -------------------------------------------------------------
    def publish(self, event: dict[str, Any]) -> dict[str, Any]:
        if "ts" not in event:
            event["ts"] = time.time()
        with self._lock:
            robot_id = event.get("robot_id")
            if isinstance(robot_id, str):
                self._append_locked(robot_id, event)
            for queue in list(self._subscribers):
                if not isinstance(robot_id, str):
                    queue.append(event)
        return event

    def status_snapshot(self, robot_id: str, status: dict[str, Any]) -> None:
        with self._lock:
            self._status[robot_id] = status

    def get_status(self, robot_id: str) -> dict[str, Any] | None:
        with self._lock:
            status = self._status.get(robot_id)
            return dict(status) if status is not None else None

    def all_status(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {rid: dict(s) for rid, s in self._status.items()}

    def history(self, robot_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            hist = self._history.get(robot_id)
            if hist is None:
                return []
            return list(hist)[-limit:]

    def subscribe(self) -> collections.deque[dict[str, Any]]:
        queue: collections.deque[dict[str, Any]] = collections.deque()
        with self._lock:
            self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: collections.deque[dict[str, Any]]) -> None:
        with self._lock:
            try:
                self._subscribers.remove(queue)
            except ValueError:
                pass
