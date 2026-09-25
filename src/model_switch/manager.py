"""Model version lifecycle: load, warm up, atomically activate, retire.

Concurrency contract
---------------------
* A request takes a **lease** on the active model: it pins a reference count
  for the duration of the lease, so an in-flight request always runs on the
  version it started with, even if a switch completes mid-request.
* A switch fully loads and warms up the candidate *before* touching the
  active slot.  The slot swap is a single pointer exchange under a lock, so
  no request can ever observe a half-loaded model.
* The retired version keeps its NumPy buffers until every lease has been
  released (reference count returns to zero); only then are they freed.
* A failed load/warm-up never mutates the active slot: service continues on
  the previous version (automatic rollback).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import numpy as np

from .errors import NoActiveModelError, SwitchBusyError
from .loader import LoadedModel, LoadReport
from .manifest import list_versions
from .model import INPUT_DIM

DEFAULT_SWITCH_TIMEOUT_S = 30.0
HISTORY_MAXLEN = 1000


@dataclass(frozen=True)
class SwitchResult:
    version: str
    generation: int
    replaced: str | None
    load_report: LoadReport
    # Reference count still held by in-flight requests on the retired model
    # at switch time; 0 means the old buffers were released immediately.
    retired_refcount: int
    elapsed_ms: float


@dataclass(frozen=True)
class Prediction:
    version: str
    generation: int
    logits: np.ndarray


@dataclass
class Lease:
    """Pins one model version for the duration of a request."""

    loaded: LoadedModel
    generation: int
    _released: bool = field(default=False)

    @property
    def version(self) -> str:
        return self.loaded.version


class ModelManager:
    def __init__(
        self,
        loader,
        *,
        switch_timeout_s: float = DEFAULT_SWITCH_TIMEOUT_S,
        clock=time.monotonic,
    ):
        self._loader = loader
        self._switch_timeout_s = switch_timeout_s
        self._clock = clock

        # Serializes whole switch attempts (load + warm-up + swap).
        self._switch_lock = threading.Lock()
        # Protects the active pointer, reference counts and retirement state.
        self._ref_lock = threading.Lock()
        self._active: LoadedModel | None = None
        self._generation = 0
        self._retired: set[LoadedModel] = set()
        self._history: deque[dict] = deque(maxlen=HISTORY_MAXLEN)

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #

    @property
    def active_version(self) -> str | None:
        with self._ref_lock:
            return self._active.version if self._active else None

    @property
    def generation(self) -> int:
        with self._ref_lock:
            return self._generation

    def status(self) -> dict:
        """Thread-safe snapshot used by the /status endpoint and tests."""
        with self._ref_lock:
            return {
                "active_version": self._active.version if self._active else None,
                "generation": self._generation,
                "active_refcount": self._active.refcount if self._active else 0,
                "retired": [
                    {
                        "version": m.version,
                        "refcount": m.refcount,
                        "disposed": m.disposed,
                    }
                    for m in sorted(self._retired, key=lambda m: m.version)
                ],
                "history": list(self._history),
            }

    def available_versions(self) -> list[str]:
        root = getattr(self._loader, "root", None)
        return list_versions(root) if root is not None else []

    # ------------------------------------------------------------------ #
    # Request path
    # ------------------------------------------------------------------ #

    @contextmanager
    def lease(self, timeout_s: float | None = None) -> Iterator[Lease]:
        """Pin the active model for one request.

        Raises :class:`SwitchBusyError` only if the active slot itself is
        held (the pointer swap); the wait is bounded and normally sub-ms
        because the swap never includes I/O or warm-up.
        """
        del timeout_s  # the swap lock is taken unconditionally below; kept
        # for API symmetry with switch_to and future tuning.
        if not self._ref_lock.acquire(timeout=self._switch_timeout_s):
            raise SwitchBusyError("timed out acquiring a model lease")
        try:
            loaded = self._active
            if loaded is None:
                raise NoActiveModelError("no model version is active")
            generation = self._generation
            loaded.acquire()
        finally:
            self._ref_lock.release()

        lease_obj = Lease(loaded=loaded, generation=generation)
        try:
            yield lease_obj
        finally:
            self._release(lease_obj)

    def predict(self, x) -> Prediction:
        arr = np.asarray(x, dtype=np.float32)
        if arr.shape[-1:] != (INPUT_DIM,):
            raise ValueError(f"last dimension must be {INPUT_DIM}, got shape {tuple(arr.shape)}")
        with self.lease() as lease_obj:
            logits = lease_obj.loaded.model.forward(arr)
            return Prediction(
                version=lease_obj.version,
                generation=lease_obj.generation,
                logits=np.array(logits, copy=True),
            )

    def _release(self, lease_obj: Lease) -> None:
        if lease_obj._released:
            return
        lease_obj._released = True
        loaded = lease_obj.loaded
        with self._ref_lock:
            remaining = loaded.release()
            retired_now = loaded in self._retired
            if remaining == 0 and retired_now and not loaded.disposed:
                loaded.dispose()
                self._retired.discard(loaded)
                self._record(
                    "released",
                    lease_obj.generation,
                    {"version": loaded.version},
                )

    # ------------------------------------------------------------------ #
    # Switch path
    # ------------------------------------------------------------------ #

    def switch_to(self, version: str) -> SwitchResult:
        """Load, verify, warm up and atomically activate ``version``.

        On any failure the previously active version keeps serving; the
        exception type identifies the cause (integrity, warm-up, ...).
        """
        started = self._clock()
        if not self._switch_lock.acquire(timeout=self._switch_timeout_s):
            raise SwitchBusyError(
                f"another switch is still in progress after {self._switch_timeout_s}s"
            )
        try:
            candidate: LoadedModel | None = None
            try:
                # Heavy lifting happens *here*, while the active slot is
                # completely untouched: requests keep being served normally.
                candidate, report = self._loader.load(version)
            except Exception as exc:
                with self._ref_lock:
                    self._record(
                        "switch_failed",
                        self._generation,
                        {"version": version, "error": f"{type(exc).__name__}: {exc}"},
                    )
                raise

            # Atomic publish: one pointer exchange, no I/O while holding it.
            with self._ref_lock:
                old = self._active
                if old is not None and old.version == version:
                    # Already active: the candidate has no other references
                    # yet (refcount == 1 from construction), so drop the
                    # manager reference and free it inline. Must not call a
                    # method that re-acquires this (non-reentrant) lock.
                    remaining = candidate.release()
                    if remaining == 0 and not candidate.disposed:
                        candidate.dispose()
                    self._record("switch_noop", self._generation, {"version": version})
                    return SwitchResult(
                        version=version,
                        generation=self._generation,
                        replaced=None,
                        load_report=report,
                        retired_refcount=0,
                        elapsed_ms=(self._clock() - started) * 1000,
                    )

                self._active = candidate
                self._generation += 1
                generation = self._generation

            retired_refcount = self._retire_old(old)
            self._record(
                "activated",
                generation,
                {"version": version, "replaced": old.version if old else None},
            )
            return SwitchResult(
                version=version,
                generation=generation,
                replaced=old.version if old else None,
                load_report=report,
                retired_refcount=retired_refcount,
                elapsed_ms=(self._clock() - started) * 1000,
            )
        finally:
            self._switch_lock.release()

    def _retire_old(self, old: LoadedModel | None) -> int:
        if old is None:
            return 0
        with self._ref_lock:
            remaining = old.release()  # drop the manager's own reference
            if remaining == 0:
                old.dispose()
                self._record("released", self._generation, {"version": old.version})
            else:
                self._retired.add(old)
                self._record(
                    "retired",
                    self._generation,
                    {"version": old.version, "refcount": remaining},
                )
            return remaining

    def _record(self, event: str, generation: int, detail: dict) -> None:
        self._history.append(
            {"t": self._clock(), "event": event, "generation": generation, **detail}
        )

    # Convenience for scripts / demos.
    def drain_retired(self, timeout_s: float = 5.0) -> bool:
        """Wait until all retired models have been released. Test/demo helper."""
        deadline = self._clock() + timeout_s
        while self._clock() < deadline:
            with self._ref_lock:
                if not self._retired:
                    return True
            time.sleep(0.005)
        return False
