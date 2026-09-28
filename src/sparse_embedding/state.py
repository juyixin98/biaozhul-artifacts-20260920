"""Training state and the sparse update transaction.

Layer 3b. This module owns the embedding table and its optimiser state and
defines the exact step rules the brief calls out:

* **Duplicate indices are aggregated first** (in the graph layer) and the
  optimiser sees each target row at most once per batch.
* **A row whose aggregated gradient is exactly all-zero takes NO optimiser
  step**: its weight, momentum buffer and per-row step counter are unchanged.
* **A row that is not present in the batch is never touched** - no momentum
  decay, no weight decay application, nothing.
* **An empty batch is an explicit no-op**: no aggregation, no clipping, no
  step and the global step counter does not move. It is reported as its own
  category, never folded into "success".

Updates are applied only to the active rows. Before those rows are written, a
sparse *before-image* (just the active rows and counters) is captured so the
whole batch can be rolled back if a later stage - e.g. persistence - fails.
The pure primitives in :mod:`graph` and :mod:`optimizer` never mutate their
inputs; the only in-place writes in the codebase happen here, on active rows,
inside this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import ClipConfig, OptimizerConfig, TableSpec
from .graph import aggregate_duplicates, clip_gradients
from .optimizer import step_rows
from .tensors import FLOAT_DTYPE, SparseGradientBatch


@dataclass(frozen=True)
class BatchReport:
    """Auditable record of one applied batch."""

    run_id: str
    batch_id: str | None
    raw_row_count: int
    unique_indices: np.ndarray
    active_indices: np.ndarray
    zero_skipped_indices: np.ndarray
    clipped_gradients: np.ndarray
    weight_deltas: np.ndarray
    clip_report: dict[str, Any]
    global_step_before: int
    global_step_after: int
    row_steps_before: np.ndarray
    row_steps_after: np.ndarray
    max_abs_delta: float

    @property
    def n_active(self) -> int:
        return int(self.active_indices.shape[0])

    @property
    def n_zero_skipped(self) -> int:
        return int(self.zero_skipped_indices.shape[0])

    def to_log_dict(self) -> dict[str, Any]:
        """JSON-serialisable summary for structured logs / test journals."""

        return {
            "run_id": self.run_id,
            "batch_id": self.batch_id,
            "raw_row_count": self.raw_row_count,
            "n_unique": int(self.unique_indices.shape[0]),
            "n_active": self.n_active,
            "n_zero_skipped": self.n_zero_skipped,
            "unique_indices": self.unique_indices.tolist(),
            "active_indices": self.active_indices.tolist(),
            "zero_skipped_indices": self.zero_skipped_indices.tolist(),
            "clip": self.clip_report,
            "global_step_before": self.global_step_before,
            "global_step_after": self.global_step_after,
            "max_abs_delta": self.max_abs_delta,
        }


class TrainingState:
    """In-memory embedding table plus sparse optimiser state."""

    def __init__(self, spec: TableSpec, seed: int = 1234) -> None:
        self.spec = spec
        rng = np.random.default_rng(seed)
        # Deterministic, bounded initialisation so numerical cross-checks are
        # reproducible. The independent dense oracle uses the SAME table values
        # (captured via ``snapshot_rows``), not a re-drawn random table.
        self._weights = rng.uniform(-0.1, 0.1, size=(spec.num_rows, spec.dim)).astype(
            FLOAT_DTYPE
        )
        self._momentum = np.zeros((spec.num_rows, spec.dim), dtype=FLOAT_DTYPE)
        self._row_steps = np.zeros(spec.num_rows, dtype=np.int64)
        self._global_step = 0
        # Rows whose state changed since the last durable commit.
        self._dirty: set[int] = set()
        # Rows that have ever taken an optimiser step.
        self._touched_ever: set[int] = set()
        # Rows seen in any batch (includes zero-gradient-only appearances).
        self._seen_ever: set[int] = set()

    # ------------------------------------------------------------------ views

    @property
    def weights(self) -> np.ndarray:
        return self._weights

    @property
    def momentum(self) -> np.ndarray:
        return self._momentum

    @property
    def row_steps(self) -> np.ndarray:
        return self._row_steps

    @property
    def global_step(self) -> int:
        return self._global_step

    @property
    def dirty_rows(self) -> frozenset[int]:
        return frozenset(self._dirty)

    @property
    def touched_rows(self) -> frozenset[int]:
        return frozenset(self._touched_ever)

    def row_view(self, index: int) -> dict[str, Any]:
        self._check_index(index)
        return {
            "index": index,
            "weight": self._weights[index].copy(),
            "momentum": self._momentum[index].copy(),
            "row_steps": int(self._row_steps[index]),
            "touched": index in self._touched_ever,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "num_rows": self.spec.num_rows,
            "dim": self.spec.dim,
            "global_step": self._global_step,
            "n_touched": len(self._touched_ever),
            "n_dirty": len(self._dirty),
            "n_seen": len(self._seen_ever),
        }

    def snapshot_rows(self, indices: np.ndarray) -> dict[str, np.ndarray]:
        """Capture weight/momentum/counter rows (shared starting point for an
        independent reference oracle). Read-only copies."""

        idx = np.asarray(indices, dtype=np.int64)
        return {
            "indices": idx.copy(),
            "weights": self._weights[idx].copy(),
            "momentum": self._momentum[idx].copy(),
            "row_steps": self._row_steps[idx].copy(),
        }

    def load_rows(
        self,
        indices: np.ndarray,
        weights: np.ndarray,
        momentum: np.ndarray,
        row_steps: np.ndarray,
        *,
        advance_global_step: int = 0,
    ) -> None:
        """Used by the checkpoint loader and rollback. Writes only given rows."""

        idx = np.asarray(indices, dtype=np.int64)
        self._check_indices(idx)
        self._weights[idx] = weights
        self._momentum[idx] = momentum
        self._row_steps[idx] = row_steps
        if advance_global_step:
            self._global_step += advance_global_step

    # ------------------------------------------------------------- public API

    def apply_batch(
        self,
        batch: SparseGradientBatch,
        optimizer_cfg: OptimizerConfig,
        clip_cfg: ClipConfig,
        *,
        run_id: str,
        batch_id: str | None = None,
    ) -> BatchReport:
        """Aggregate, clip and apply one sparse batch as a single transaction.

        Raises :class:`EmptyBatchError` for an empty batch without changing any
        state. Raises :class:`ValidationBatchRejectedError` for structural
        problems before any write. On a post-step numerical failure the sparse
        before-image is restored and the error propagates.
        """

        # Structural validation (including full index range check) happens in
        # the SparseGradientBatch constructor; empty batch is explicit.
        batch.require_non_empty()

        # ---- pure stages: aggregation then declared clipping mode ----------
        agg = aggregate_duplicates(batch)
        clipped, clip_report = clip_gradients(agg, clip_cfg)

        unique = agg.unique_indices
        active_mask = np.any(clipped != 0.0, axis=1)
        active_idx = unique[active_mask]
        zero_idx = unique[~active_mask]

        # ---- sparse before-image (only rows about to be written) -----------
        before_weights = self._weights[active_idx].copy() if active_idx.size else None
        before_momentum = self._momentum[active_idx].copy() if active_idx.size else None
        before_steps = self._row_steps[active_idx].copy() if active_idx.size else None
        global_before = self._global_step

        try:
            if active_idx.size:
                grad_active = clipped[active_mask]
                w_new, m_new = step_rows(
                    self._weights[active_idx],
                    self._momentum[active_idx],
                    grad_active,
                    optimizer_cfg,
                )
                if not (np.isfinite(w_new).all() and np.isfinite(m_new).all()):
                    raise FloatingPointError(
                        "non-finite values produced by optimiser step; rolling back"
                    )
                self._weights[active_idx] = w_new
                self._momentum[active_idx] = m_new
                self._row_steps[active_idx] = self._row_steps[active_idx] + 1
                self._dirty.update(int(i) for i in active_idx.tolist())
                self._touched_ever.update(int(i) for i in active_idx.tolist())
                deltas = w_new - before_weights
            else:
                deltas = np.empty((0, self.spec.dim), dtype=FLOAT_DTYPE)

            self._seen_ever.update(int(i) for i in unique.tolist())
            # A non-empty batch is one update round even if every aggregated
            # gradient was zero; per-row counters distinguish real steps.
            self._global_step += 1
        except Exception:
            # Roll the active rows back to their before-image; nothing else was
            # ever written, so untouched and zero rows need no restoration.
            if active_idx.size:
                self._weights[active_idx] = before_weights
                self._momentum[active_idx] = before_momentum
                self._row_steps[active_idx] = before_steps
            self._global_step = global_before
            raise

        return BatchReport(
            run_id=run_id,
            batch_id=batch_id,
            raw_row_count=batch.size,
            unique_indices=unique,
            active_indices=active_idx,
            zero_skipped_indices=zero_idx,
            clipped_gradients=clipped,
            weight_deltas=deltas,
            clip_report=clip_report,
            global_step_before=global_before,
            global_step_after=self._global_step,
            row_steps_before=before_steps if before_steps is not None else np.empty(0, np.int64),
            row_steps_after=self._row_steps[active_idx].copy() if active_idx.size else np.empty(0, np.int64),
            max_abs_delta=float(np.max(np.abs(deltas))) if deltas.size else 0.0,
        )

    def mark_clean(self, rows: frozenset[int] | None = None) -> None:
        """Clear dirty markers after a durable checkpoint has committed."""

        if rows is None:
            self._dirty.clear()
        else:
            self._dirty -= set(rows)

    # ------------------------------------------------------------- internals

    def _check_index(self, index: int) -> None:
        if index < 0 or index >= self.spec.num_rows:
            raise IndexError(
                f"row index {index} out of range for table of {self.spec.num_rows} rows"
            )

    def _check_indices(self, idx: np.ndarray) -> None:
        if idx.size and (int(idx.min()) < 0 or int(idx.max()) >= self.spec.num_rows):
            raise IndexError("row index out of range while loading state")
