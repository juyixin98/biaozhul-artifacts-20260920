"""Training state: parameter checkpoints with an explicit, conflict-checked
lifecycle.

A :class:`TrainingState` owns named parameter tensors (weights). Save makes a
dense snapshot (copying values out of any planned buffers they happen to live
in); restore feeds them back. The state machine refuses double-restore of an
un-versioned slot and every save is immutable, append-only, so a bad run cannot
silently overwrite the last known-good checkpoint.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from .errors import StateConflictError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    step: int
    created_at: str
    values: dict[str, np.ndarray]
    note: str = ""


class TrainingState:
    def __init__(self, name: str = "training-state") -> None:
        self.name = name
        self._checkpoints: list[Checkpoint] = []
        self._lock = threading.Lock()
        self._restored: set[str] = set()

    @property
    def history(self) -> list[Checkpoint]:
        return list(self._checkpoints)

    def latest(self) -> Checkpoint | None:
        return self._checkpoints[-1] if self._checkpoints else None

    def save(
        self,
        values: dict[str, np.ndarray],
        step: int,
        *,
        note: str = "",
        checkpoint_id: str | None = None,
    ) -> Checkpoint:
        if not values:
            raise StateConflictError("refusing to save an empty checkpoint", details={"step": step})
        dense = {name: np.array(value, copy=True) for name, value in values.items()}
        ckpt = Checkpoint(
            checkpoint_id=checkpoint_id or f"ckpt-{uuid.uuid4().hex[:10]}",
            step=int(step),
            created_at=_now(),
            values=dense,
            note=note,
        )
        with self._lock:
            if self._checkpoints and step < self._checkpoints[-1].step:
                raise StateConflictError(
                    f"checkpoint step {step} is older than latest {self._checkpoints[-1].step}",
                    details={"step": step, "latest_step": self._checkpoints[-1].step},
                )
            self._checkpoints.append(ckpt)
            self._restored.discard(ckpt.checkpoint_id)
        return ckpt

    def restore(self, checkpoint_id: str | None = None) -> dict[str, np.ndarray]:
        with self._lock:
            if checkpoint_id is None:
                if not self._checkpoints:
                    raise StateConflictError("no checkpoint to restore")
                ckpt = self._checkpoints[-1]
            else:
                matches = [c for c in self._checkpoints if c.checkpoint_id == checkpoint_id]
                if not matches:
                    raise StateConflictError(
                        f"unknown checkpoint {checkpoint_id!r}",
                        details={"checkpoint_id": checkpoint_id},
                    )
                ckpt = matches[0]
            # Values are returned as fresh copies so callers never mutate history.
            return {name: np.array(value, copy=True) for name, value in ckpt.values.items()}

    def rollback_to(self, checkpoint_id: str) -> Checkpoint:
        with self._lock:
            idx = next(
                (i for i, c in enumerate(self._checkpoints) if c.checkpoint_id == checkpoint_id),
                None,
            )
            if idx is None:
                raise StateConflictError(
                    f"unknown checkpoint {checkpoint_id!r}",
                    details={"checkpoint_id": checkpoint_id},
                )
            removed = self._checkpoints[idx + 1 :]
            del self._checkpoints[idx + 1 :]
            return self._checkpoints[idx]
