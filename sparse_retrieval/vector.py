"""Sparse vector type with explicit normalization semantics.

Semantics (also documented in README):

- Input is an iterable of ``(dim, weight)`` pairs.
- Duplicate dimensions are merged by *summing* their weights.
- Entries whose merged weight is exactly ``0.0`` are dropped.
- A vector with no remaining entries is the *zero vector*: its L2 norm is
  ``0.0`` and its cosine similarity with any vector is defined as ``0.0``.
- Negative weights are first-class; cosine scores may be negative.
"""

from __future__ import annotations

import math
from typing import Iterable, Iterator, Tuple


class SparseVector:
    """Immutable sparse vector over non-negative integer dimensions."""

    __slots__ = ("_entries", "_norm", "duplicates_merged")

    def __init__(self, pairs: Iterable[Tuple[int, float]] = ()) -> None:
        merged: dict[int, float] = {}
        duplicates_merged = 0
        for dim, weight in pairs:
            d = self._validate_dim(dim)
            w = self._validate_weight(weight)
            if d in merged:
                duplicates_merged += 1
            merged[d] = merged.get(d, 0.0) + w
        entries = {d: w for d, w in merged.items() if w != 0.0}
        # Store as a sorted-items tuple for deterministic iteration order.
        self._entries: Tuple[Tuple[int, float], ...] = tuple(sorted(entries.items()))
        self._norm = math.sqrt(sum(w * w for _, w in self._entries))
        self.duplicates_merged = duplicates_merged

    @staticmethod
    def _validate_dim(dim: object) -> int:
        if isinstance(dim, bool) or not isinstance(dim, int):
            raise ValueError(f"dimension must be an int, got {dim!r}")
        if dim < 0:
            raise ValueError(f"dimension must be >= 0, got {dim}")
        return dim

    @staticmethod
    def _validate_weight(weight: object) -> float:
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise ValueError(f"weight must be a number, got {weight!r}")
        w = float(weight)
        if not math.isfinite(w):
            raise ValueError(f"weight must be finite, got {weight!r}")
        return w

    @property
    def norm(self) -> float:
        """L2 norm; ``0.0`` for the zero vector."""
        return self._norm

    @property
    def nnz(self) -> int:
        """Number of stored (non-zero) entries."""
        return len(self._entries)

    @property
    def is_zero(self) -> bool:
        return self._norm == 0.0

    @property
    def entries(self) -> Tuple[Tuple[int, float], ...]:
        """``((dim, weight), ...)`` sorted by dimension, weights non-zero."""
        return self._entries

    def as_dict(self) -> dict[int, float]:
        return dict(self._entries)

    def __iter__(self) -> Iterator[Tuple[int, float]]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SparseVector):
            return NotImplemented
        return self._entries == other._entries

    def __repr__(self) -> str:
        return f"SparseVector(nnz={self.nnz}, norm={self._norm:.6g})"
