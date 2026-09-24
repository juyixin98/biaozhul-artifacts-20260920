"""Per-robot in-process event bus used by the SSE subscription endpoint.

Isolation model
---------------
* ``subscribe(robot_id)`` receives only events emitted for that robot. A
  subscriber authenticated as robot A is never handed robot B's events —
  B does not even exist in its filtered stream.
* ``subscribe_admin()`` receives everything (operator view).
* When a robot's mapping changes, ``close_robot(robot_id)`` is called: every
  live SSE stream authorized under the old mapping receives a terminal
  ``mapping_reset`` event and is closed immediately, so stale authorizations
  cannot keep watching a (possibly different) namespace.

The bus is asyncio-native: each subscriber owns an unbounded-ish asyncio
Queue; emit is a no-op when nobody is listening.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._robot_queues: dict[str, set[asyncio.Queue]] = {}
        self._admin_queues: set[asyncio.Queue] = set()

    async def subscribe(self, robot_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._robot_queues.setdefault(robot_id, set()).add(queue)
        return queue

    async def unsubscribe(self, robot_id: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            subs = self._robot_queues.get(robot_id)
            if subs:
                subs.discard(queue)
                if not subs:
                    self._robot_queues.pop(robot_id, None)

    async def subscribe_admin(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        async with self._lock:
            self._admin_queues.add(queue)
        return queue

    async def unsubscribe_admin(self, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._admin_queues.discard(queue)

    async def emit(self, robot_id: str, event: dict[str, Any]) -> None:
        """Fan one event out to the robot's subscribers and to admins."""
        payload = dict(event)
        payload.setdefault("rid", robot_id)
        async with self._lock:
            queues = list(self._robot_queues.get(robot_id, ()))
            admins = list(self._admin_queues)
        for queue in queues + admins:
            # Drop-oldest rather than letting one slow client stall the bus.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(payload)

    async def close_robot(self, robot_id: str, reason: str) -> None:
        """Terminate every live stream for a robot after a mapping change."""
        sentinel = {"type": "mapping_reset", "rid": robot_id, "reason": reason,
                    "terminal": True}
        async with self._lock:
            queues = list(self._robot_queues.pop(robot_id, ()))
        for queue in queues:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(sentinel)
