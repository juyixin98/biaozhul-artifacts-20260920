"""Tensor types: the validated value objects that cross layer boundaries.

Layer 1 of the engineering layout. Nothing here performs optimisation; these
types only *describe* tensors and enforce the structural invariants every
later layer relies on (dtype, shape, matching lengths, in-range indices).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import TableSpec
from .errors import EmptyBatchError, ValidationBatchRejectedError

# A single canonical floating dtype for all state and arithmetic. Keeping one
# dtype removes a whole class of silent up/down-casting bugs.
FLOAT_DTYPE = np.dtype(np.float64)
# Indices are 64-bit signed; negative values are structurally impossible and
# therefore immediately visible as out-of-range.
INDEX_DTYPE = np.dtype(np.int64)


def _as_2d_float(values: np.ndarray, *, field_name: str, dim: int) -> np.ndarray:
    arr = np.asarray(values)
    # A canonical empty batch may arrive as a 1-D empty array/list. Normalise it
    # to ``(0, dim)`` rather than rejecting it.
    if arr.ndim == 1 and arr.size == 0:
        arr = np.empty((0, dim), dtype=FLOAT_DTYPE)
    if arr.ndim != 2:
        raise ValidationBatchRejectedError(
            f"{field_name} must be a 2-D array of shape (n, dim), got shape {arr.shape}",
            details={"field": field_name, "shape": list(arr.shape)},
        )
    if not np.issubdtype(arr.dtype, np.number):
        raise ValidationBatchRejectedError(
            f"{field_name} must contain numeric values, got dtype {arr.dtype}",
            details={"field": field_name, "dtype": str(arr.dtype)},
        )
    return np.ascontiguousarray(arr, dtype=FLOAT_DTYPE)


def _as_1d_index(indices: np.ndarray) -> np.ndarray:
    arr = np.asarray(indices)
    if arr.ndim != 1:
        raise ValidationBatchRejectedError(
            f"indices must be a 1-D array, got shape {arr.shape}",
            details={"shape": list(arr.shape)},
        )
    # An empty index list may carry the default float dtype; normalise it to
    # the canonical integer dtype rather than rejecting an empty batch here.
    if arr.size == 0:
        return np.empty((0,), dtype=INDEX_DTYPE)
    if not np.issubdtype(arr.dtype, np.integer):
        raise ValidationBatchRejectedError(
            f"indices must contain integers, got dtype {arr.dtype}",
            details={"dtype": str(arr.dtype)},
        )
    return np.ascontiguousarray(arr, dtype=INDEX_DTYPE)


@dataclass(frozen=True)
class SparseGradientBatch:
    """Raw sparse gradient rows as presented by a caller.

    ``indices[i]`` names the embedding row for the gradient ``values[i]``.
    Duplicate indices are expected and legal; they are summed during
    aggregation. The batch is fully validated on construction, including the
    range of *every* index against the table — a single out-of-range index
    rejects the entire batch.
    """

    indices: np.ndarray
    values: np.ndarray
    num_rows: int
    dim: int

    def __post_init__(self) -> None:
        indices = _as_1d_index(self.indices)
        values = _as_2d_float(self.values, field_name="values", dim=self.dim)

        if indices.shape[0] != values.shape[0]:
            raise ValidationBatchRejectedError(
                "indices and values must have the same number of rows",
                details={"n_indices": int(indices.shape[0]), "n_values": int(values.shape[0])},
            )

        n = indices.shape[0]
        if n > 0 and values.shape[1] != self.dim:
            raise ValidationBatchRejectedError(
                "gradient width does not match table dim",
                details={"expected_dim": self.dim, "got_dim": int(values.shape[1])},
            )

        # Whole-batch index range check: performed BEFORE anything downstream
        # runs, so one bad index rejects the entire batch with no partial work.
        if n > 0:
            min_idx = int(indices.min())
            max_idx = int(indices.max())
            if min_idx < 0 or max_idx >= self.num_rows:
                bad = indices[(indices < 0) | (indices >= self.num_rows)]
                sample = int(bad[0]) if bad.size else max_idx
                raise ValidationBatchRejectedError(
                    "index out of range: the entire batch is rejected",
                    details={
                        "num_rows": self.num_rows,
                        "bad_index": sample,
                        "n_bad": int(bad.size),
                    },
                )
            if not np.isfinite(values).all():
                raise ValidationBatchRejectedError(
                    "gradient values must all be finite (no NaN/Inf)",
                    details={"n_non_finite": int((~np.isfinite(values)).sum())},
                )

        object.__setattr__(self, "indices", indices)
        object.__setattr__(self, "values", values)

    @property
    def size(self) -> int:
        return int(self.indices.shape[0])

    def require_non_empty(self) -> None:
        if self.size == 0:
            raise EmptyBatchError(
                "empty batch: no rows to update (no aggregation, no optimizer step)",
                details={"num_rows": self.num_rows, "dim": self.dim},
            )

    @classmethod
    def from_pairs(
        cls,
        indices: np.ndarray | list[int],
        values: np.ndarray | list[list[float]],
        spec: TableSpec,
    ) -> "SparseGradientBatch":
        # IMPORTANT: indices are passed through WITHOUT a target dtype so a
        # float index array (e.g. [0.5, 1.0]) is rejected by the integer check
        # rather than silently truncated to [0, 1].
        return cls(
            indices=np.asarray(indices),
            values=np.asarray(values, dtype=FLOAT_DTYPE),
            num_rows=spec.num_rows,
            dim=spec.dim,
        )


@dataclass(frozen=True)
class AggregatedSparseGradient:
    """The result of coalescing duplicate indices.

    ``unique_indices`` is sorted ascending with no repeats. ``aggregated[k]``
    is the *sum* of every raw gradient row targeting ``unique_indices[k]``.
    ``row_norms[k]`` is the per-touched-row L2 norm used by row clipping;
    ``global_norm`` is the single Frobenius norm used by global clipping.
    The two norms are computed once and kept distinct so the two clip modes
    can never be mixed.
    """

    unique_indices: np.ndarray
    aggregated: np.ndarray
    row_norms: np.ndarray
    global_norm: float
    raw_count: int

    def __post_init__(self) -> None:
        if self.unique_indices.ndim != 1:
            raise TypeError("unique_indices must be 1-D")
        if self.aggregated.ndim != 2:
            raise TypeError("aggregated must be 2-D")
        if self.unique_indices.shape[0] != self.aggregated.shape[0]:
            raise ValueError("unique_indices and aggregated length mismatch")
        if self.row_norms.shape[0] != self.aggregated.shape[0]:
            raise ValueError("row_norms length mismatch")

    @property
    def touched_count(self) -> int:
        return int(self.unique_indices.shape[0])
