"""Atomic version switching with reference-counted retirement.

Design:

- The registry holds at most one ACTIVE slot (serving new requests) and at
  most one STANDBY slot (the previously active version, kept loaded so
  `rollback()` is instantaneous). Older versions are RETIRED.
- A request takes a `Lease` on the current slot. The lease pins the slot's
  model: as long as any lease is held, the version's resources are never
  released, so in-flight requests always finish on the version they started
  with — they never see half-loaded or freed weights.
- `switch()` is a single pointer swap under one lock. It never mutates the
  old slot's model. The old slot becomes STANDBY; the previous STANDBY (if
  any) becomes RETIRED.
- A RETIRED slot's model is closed exactly when its lease count reaches
  zero — immediately if no requests are in flight, otherwise when the last
  in-flight request releases its lease.

All state transitions happen under one lock; there is no window in which a
reader can observe a partially switched registry.
"""
from __future__ import annotations

import enum
import threading

from .loader import ModelVersion


class NoActiveModelError(Exception):
    """acquire() was called before any version was activated."""


class RollbackUnavailableError(Exception):
    """rollback() was called with no standby version available."""


class _SlotState(enum.Enum):
    ACTIVE = "active"
    STANDBY = "standby"
    RETIRED = "retired"


class _Slot:
    __slots__ = ("version", "state", "inflight")

    def __init__(self, version: ModelVersion, state: _SlotState) -> None:
        self.version = version
        self.state = state
        self.inflight = 0

    def snapshot(self) -> dict:
        return {
            "version": self.version.version,
            "state": self.state.value,
            "inflight": self.inflight,
            "closed": self.version.closed,
        }


class Lease:
    """A held reference to a slot. Use as a context manager or call close().

    The lease guarantees `lease.model` stays usable until `close()` returns,
    regardless of any concurrent switch/rollback.
    """

    def __init__(self, registry: "ModelRegistry", slot: _Slot) -> None:
        self._registry = registry
        self._slot = slot
        self._released = False

    @property
    def model(self) -> ModelVersion:
        if self._released:
            raise RuntimeError("lease already released")
        return self._slot.version

    @property
    def version(self) -> str:
        return self._slot.version.version

    def close(self) -> None:
        if not self._released:
            self._released = True
            self._registry._release(self._slot)

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class ModelRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: _Slot | None = None
        self._standby: _Slot | None = None
        self._retired: list[_Slot] = []

    # -- request path ------------------------------------------------------

    def acquire(self) -> Lease:
        """Pin the currently active version for the duration of a request."""
        with self._lock:
            if self._current is None:
                raise NoActiveModelError("no model version is active")
            self._current.inflight += 1
            return Lease(self, self._current)

    def _release(self, slot: _Slot) -> None:
        with self._lock:
            slot.inflight -= 1
            if slot.inflight < 0:  # pragma: no cover - defensive
                raise RuntimeError("lease released more times than acquired")
            self._maybe_finalize(slot)

    # -- admin path --------------------------------------------------------

    def switch(self, version: ModelVersion) -> None:
        """Atomically make `version` the active model.

        The caller is expected to pass a fully warmed-up candidate from
        `load_candidate`. The swap itself is O(1) and never blocks readers.
        """
        new_slot = _Slot(version, _SlotState.ACTIVE)
        with self._lock:
            old_current = self._current
            old_standby = self._standby
            self._current = new_slot
            self._standby = old_current
            if old_current is not None:
                old_current.state = _SlotState.STANDBY
            if old_standby is not None:
                old_standby.state = _SlotState.RETIRED
                self._retired.append(old_standby)
                self._maybe_finalize(old_standby)

    def rollback(self) -> str:
        """Atomically swap active and standby. Returns the new active version."""
        with self._lock:
            if self._standby is None:
                raise RollbackUnavailableError("no standby version to roll back to")
            self._current, self._standby = self._standby, self._current
            assert self._current is not None and self._standby is not None
            self._current.state = _SlotState.ACTIVE
            self._standby.state = _SlotState.STANDBY
            return self._current.version.version

    # -- introspection -----------------------------------------------------

    def current_version(self) -> str | None:
        with self._lock:
            return self._current.version.version if self._current else None

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "active": self._current.snapshot() if self._current else None,
                "standby": self._standby.snapshot() if self._standby else None,
                "retired": [slot.snapshot() for slot in self._retired],
            }

    # -- internals -----------------------------------------------------------

    def _maybe_finalize(self, slot: _Slot) -> None:
        """Close a retired slot whose last in-flight request has finished.

        Must be called with the lock held. Resource release happens exactly
        here: refcount zero AND retired.
        """
        if slot.state is _SlotState.RETIRED and slot.inflight == 0:
            slot.version.close()
            if slot in self._retired:
                self._retired.remove(slot)
