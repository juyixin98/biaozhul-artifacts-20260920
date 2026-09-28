"""Symbolic Cholesky factorization.

Given the (already permuted) sparsity pattern of a symmetric matrix, the
symbolic phase predicts the exact nonzero structure of the unit-lower factor
``L`` in ``A = L D L^T`` *without touching a single numerical value*.

Algorithm
---------
Columns are processed left to right.  For each column ``k`` we merge:

* the lower-triangular entries of ``A[:, k]``, and
* the sub-diagonal pattern of every earlier column ``j < k`` for which
  ``L[k, j]`` is nonzero.

Those earlier columns are enumerated through per-row lists threaded while
each column is finalized.  Because every edge ``(r, j)`` is pushed exactly
once (when column ``j`` is built) and popped exactly once (when column ``r``
is built), the total work and extra storage is ``O(nnz(L))``; the matrix is
never densified.

This relies on the central structural identity

    L[:, k] = A[:, k]  U  union { L[:, j] : j < k, L[k, j] != 0 },

which also underlies the numeric left-looking kernel, so both phases iterate
the identical structure.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

import numpy as np
from scipy import sparse

from .etree import NO_PARENT, elimination_tree, tree_height


@dataclass(frozen=True)
class SymbolicFactor:
    """The predicted structure of ``L``.

    Attributes
    ----------
    n:
        Matrix order.
    parent:
        Elimination tree (``parent[i] = -1`` for roots).
    col_rows:
        ``col_rows[k]`` is the sorted array of row indices ``i >= k`` that
        are structurally nonzero in column ``k`` of ``L`` (includes ``k``).
    row_cols:
        Mirror lists: ``row_cols[i]`` contains all ``j < i`` with
        ``L[i, j] != 0``, in ascending order.  Used by the numeric kernel.
    col_ptr, row_idx:
        Same column pattern packed in CSR/CSC-style index arrays for the
        lower factor (diagonal included).
    nnz_lower:
        Number of nonzeros in the lower factor including the unit diagonal.
    nnz_a_lower:
        Number of nonzeros in the lower triangle of the input pattern.
    etree_height:
        Height of the elimination tree (parallelism/chain indicator).
    """

    n: int
    parent: np.ndarray
    col_rows: list[np.ndarray]
    row_cols: list[list[int]]
    col_ptr: np.ndarray
    row_idx: np.ndarray
    nnz_lower: int
    nnz_a_lower: int
    etree_height: int

    @property
    def fill_in(self) -> int:
        """New lower-triangular nonzeros introduced by factorization."""
        return self.nnz_lower - self.nnz_a_lower

    @property
    def fill_ratio(self) -> float:
        if self.nnz_a_lower == 0:
            return 0.0
        return self.nnz_lower / self.nnz_a_lower

    def structure_pattern_lower(self) -> tuple[np.ndarray, np.ndarray]:
        """Expose (row_idx, col_ptr) of the L pattern for comparisons."""
        return self.row_idx, self.col_ptr

    def rows_below(self, col: int) -> np.ndarray:
        """Rows of L[:, col] strictly below the diagonal."""
        rows = self.col_rows[col]
        return rows[bisect_right(rows, col):]


def symbolic_factorization(matrix_csc: sparse.csc_matrix) -> SymbolicFactor:
    """Compute the elimination tree and the exact Cholesky fill pattern.

    Parameters
    ----------
    matrix_csc:
        Symmetric sparse matrix (already permuted into the desired elimination
        order).  Only its *structure* is read.
    """
    n = matrix_csc.shape[0]
    parent = elimination_tree(matrix_csc)

    indptr, indices = matrix_csc.indptr, matrix_csc.indices

    # Lower-triangular entries of A, grouped per column, sorted.
    a_lower: list[np.ndarray] = [np.empty(0, dtype=np.int64) for _ in range(n)]
    nnz_a_lower = 0
    for k in range(n):
        members = indices[indptr[k]:indptr[k + 1]]
        lower = members[members >= k].astype(np.int64, copy=True)
        lower.sort()
        a_lower[k] = lower
        nnz_a_lower += lower.size

    # Per-row lists of columns already known to reach that row.
    row_cols: list[list[int]] = [[] for _ in range(n)]
    col_rows: list[np.ndarray] = [np.empty(0, dtype=np.int64)] * n

    nnz_lower = 0
    col_ptr = np.empty(n + 1, dtype=np.int64)
    packed_rows: list[np.ndarray] = []

    for k in range(n):
        # Seed with A's own lower pattern (always includes diagonal).
        merged = set(a_lower[k].tolist())
        merged.add(k)

        # Merge sub-diagonal patterns of all earlier columns j with L[k,j]!=0.
        for j in row_cols[k]:
            prior = col_rows[j]
            cut = bisect_right(prior, k)
            merged.update(prior[cut:].tolist())

        rows = np.fromiter(merged, dtype=np.int64, count=len(merged))
        rows.sort()
        col_rows[k] = rows

        col_ptr[k] = nnz_lower
        packed_rows.append(rows)
        nnz_lower += rows.size

        # Thread this column onto the row lists of its sub-diagonal entries.
        for r in rows:
            if r > k:
                row_cols[int(r)].append(k)

    col_ptr[n] = nnz_lower
    row_idx = (
        np.concatenate(packed_rows) if packed_rows else np.empty(0, dtype=np.int64)
    )

    return SymbolicFactor(
        n=n,
        parent=parent,
        col_rows=col_rows,
        row_cols=row_cols,
        col_ptr=col_ptr,
        row_idx=row_idx,
        nnz_lower=int(nnz_lower),
        nnz_a_lower=int(nnz_a_lower),
        etree_height=tree_height(parent),
    )


def reach_within_pattern(symbolic: SymbolicFactor, col: int) -> int:
    """Number of sub-diagonal nonzeros predicted for one column."""
    return symbolic.col_rows[col].size - 1


def roots_of_etree(parent: np.ndarray) -> np.ndarray:
    """Indices of elimination-tree roots (independent block leaders)."""
    return np.flatnonzero(parent == NO_PARENT).astype(np.int64)
