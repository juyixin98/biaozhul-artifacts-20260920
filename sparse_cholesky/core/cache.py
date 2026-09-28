"""Cache for reusable symbolic factorizations.

A symbolic structure describes only the sparsity pattern.  It is safe to
reuse with a different matrix **iff the sparsity patterns are identical** --
reusing it on a matrix that merely has the same order, or a subset/superset
of entries, would make the numeric kernel skip a real nonzero or visit a
missing one.  Reuse is therefore gated on an exact structural fingerprint
(the sorted lower-triangular (row, col) pairs), never on dimensions alone.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np
from scipy import sparse

from sparse_cholesky.input.errors import SymbolicStructureMismatchError

from .ordering import Permutation
from .symbolic import SymbolicFactor


def pattern_fingerprint(matrix_csc: sparse.csc_matrix) -> tuple[int, bytes]:
    """Canonical fingerprint of the lower-triangular sparsity pattern.

    Returns ``(n, bytes)`` where the bytes encode sorted (row, col) index
    pairs of entries on or below the diagonal.  Values play no role: two
    matrices with identical support share a fingerprint regardless of
    numerical content.
    """
    n = matrix_csc.shape[0]
    coo = matrix_csc.tocoo()
    mask = coo.row >= coo.col
    rows = coo.row[mask].astype(np.int64)
    cols = coo.col[mask].astype(np.int64)
    order = np.lexsort((cols, rows))
    packed = np.empty(rows.size * 2, dtype=np.int64)
    packed[0::2] = rows[order]
    packed[1::2] = cols[order]
    return n, packed.tobytes()


@dataclass(frozen=True)
class CachedSymbolic:
    fingerprint: tuple[int, bytes]
    ordering_name: str
    permutation: Permutation
    symbolic: SymbolicFactor


class SymbolicCache:
    """Thread-safe map ``(fingerprint, ordering) -> CachedSymbolic``."""

    def __init__(self) -> None:
        self._store: dict[tuple[int, bytes, str], CachedSymbolic] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def key(self, matrix_csc: sparse.csc_matrix, ordering_name: str):
        n, fp = pattern_fingerprint(matrix_csc)
        return n, fp, ordering_name

    def get(self, matrix_csc: sparse.csc_matrix,
            ordering_name: str) -> CachedSymbolic | None:
        with self._lock:
            cached = self._store.get(self.key(matrix_csc, ordering_name))
            if cached is None:
                self.misses += 1
                return None
            self.hits += 1
            return cached

    def put(self, matrix_csc: sparse.csc_matrix, ordering_name: str,
            permutation: Permutation, symbolic: SymbolicFactor) -> CachedSymbolic:
        cached = CachedSymbolic(
            fingerprint=pattern_fingerprint(matrix_csc),
            ordering_name=ordering_name,
            permutation=permutation,
            symbolic=symbolic,
        )
        with self._lock:
            self._store[self.key(matrix_csc, ordering_name)] = cached
        return cached

    def require_match(self, cached: CachedSymbolic,
                      matrix_csc: sparse.csc_matrix) -> None:
        """Raise unless ``matrix_csc`` has exactly the cached pattern."""
        actual = pattern_fingerprint(matrix_csc)
        if actual != cached.fingerprint:
            raise SymbolicStructureMismatchError(
                "refusing to reuse symbolic structure: sparsity pattern "
                "differs from the cached factorization"
            )

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self.hits = 0
            self.misses = 0
