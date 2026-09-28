"""Service facade: the single object the HTTP layer and scripts drive.

Layer 5b. It composes config, training state, persistence and the journal,
guards state with a lock, and maps domain outcomes to explicit verdicts. It
contains no NumPy maths of its own - all computation stays in the lower
layers, which keeps the service thin and the core independently testable.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np

from .config import ServiceConfig
from .errors import (
    EmptyBatchError,
    PersistenceError,
    SparseUpdateError,
    ValidationBatchRejectedError,
)
from .journal import RunJournal, new_run_id
from .persistence import CheckpointStore
from .state import BatchReport, TrainingState
from .tensors import FLOAT_DTYPE, SparseGradientBatch

# Stages, in order. The journal reports exactly which ones ran, which makes
# the decision basis (aggregation? clipping? step?) auditable per request.
STAGES_VALIDATE = ["validate"]
STAGES_APPLY = ["validate", "aggregate", "clip", "optimizer_step"]
STAGES_EMPTY = ["validate"]


class SparseEmbeddingService:
    def __init__(self, config: ServiceConfig, journal: RunJournal | None = None) -> None:
        self.config = config
        self.journal = journal or RunJournal()
        self.store = CheckpointStore(config.state_dir)
        self._lock = threading.RLock()
        self.state = TrainingState(config.table, seed=config.seed)

    # ------------------------------------------------------------------ load

    def load_if_present(self) -> bool:
        with self._lock:
            if self.store.exists():
                self.state = self.store.load(self.config)
                return True
            return False

    # ----------------------------------------------------------------- apply

    def apply(
        self,
        indices,
        values,
        *,
        run_id: str | None = None,
        batch_id: str | None = None,
    ) -> BatchReport:
        """Validate, aggregate, clip and step. Never maps errors to success."""

        run_id = run_id or new_run_id()
        start = time.perf_counter()
        try:
            batch = SparseGradientBatch.from_pairs(
                indices, values, self.config.table
            )
        except ValidationBatchRejectedError as exc:
            # Whole batch rejected before any aggregation or write.
            self._journal(
                run_id, "apply_batch", "rejected", STAGES_VALIDATE, batch_id,
                details={"n_indices": _safe_len(indices), **exc.details},
                error_code=exc.code, start=start,
            )
            raise

        # Empty batch is an explicit no-op category, handled before the lock
        # and before any aggregation / clipping / step.
        try:
            batch.require_non_empty()
        except EmptyBatchError as exc:
            self._journal(
                run_id, "apply_batch", "empty", STAGES_EMPTY, batch_id,
                details=exc.details, error_code=exc.code, start=start,
            )
            raise

        with self._lock:
            try:
                report = self.state.apply_batch(
                    batch,
                    self.config.optimizer,
                    self.config.clip,
                    run_id=run_id,
                    batch_id=batch_id,
                )
            except SparseUpdateError:
                raise
            except Exception as exc:  # numerical/rollback failure: explicit error
                self._journal(
                    run_id, "apply_batch", "error", STAGES_APPLY, batch_id,
                    details={"error": repr(exc)}, error_code="internal_error",
                    start=start,
                )
                raise

        verdict = "applied"
        details = report.to_log_dict()
        # Make the decision basis explicit when a non-empty batch only touched
        # zero-gradient rows.
        if report.n_active == 0:
            verdict = "applied_zero_only"
        self._journal(
            run_id, "apply_batch", verdict, STAGES_APPLY, batch_id,
            details=details, start=start,
        )
        return report

    # -------------------------------------------------------------- persist

    def checkpoint(self, *, run_id: str | None = None) -> dict[str, Any]:
        run_id = run_id or new_run_id()
        start = time.perf_counter()
        with self._lock:
            try:
                manifest = self.store.save(self.state, self.config)
            except PersistenceError:
                self._journal(
                    run_id, "checkpoint", "error", ["persist"], None,
                    details={"state_dir": self.config.state_dir},
                    error_code="persistence_error", start=start,
                )
                raise
        self._journal(
            run_id, "checkpoint", "committed", ["persist"], None,
            details=manifest.to_dict(), start=start,
        )
        return {
            "run_id": run_id,
            "verdict": "committed",
            "persisted_touched_rows": manifest.n_touched,
            "global_step": manifest.global_step,
            "state_dir": self.config.state_dir,
        }

    # ----------------------------------------------------------------- views

    def row(self, index: int) -> dict[str, Any]:
        with self._lock:
            return self.state.row_view(index)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return self.state.summary()

    # ------------------------------------------------------------------ misc

    @staticmethod
    def to_numpy(values) -> np.ndarray:
        return np.asarray(values, dtype=FLOAT_DTYPE)

    def _journal(self, run_id, event, verdict, stages, batch_id, *,
                 details=None, error_code=None, start=None) -> None:
        elapsed = None if start is None else (time.perf_counter() - start) * 1000.0
        self.journal.record(
            run_id=run_id,
            event=event,
            verdict=verdict,
            stages=stages,
            batch_id=batch_id,
            details=details,
            error_code=error_code,
            elapsed_ms=elapsed,
        )


def _safe_len(x: Any) -> int | None:
    try:
        return len(x)
    except TypeError:
        return None
