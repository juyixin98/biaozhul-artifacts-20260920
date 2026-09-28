"""Symbolic factorization.

Runs *before* any numerical work and is reusable across numerical values
that share the exact same sparsity pattern.

Given a permutation ``perm`` (perm[k] = original node eliminated at k),
we form B = P A P^T and compute:

1. the elimination tree (Liu's forest-union algorithm);
2. the exact row structure of every column of L (B = L D L^T) by
   subtree walks on the elimination tree;
3. fill statistics (L nnz vs original nnz).

Everything is index/sparse work; no floating-point matrix is formed.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp

from .ordering import OrderingResult

NO_PARENT = -1


@dataclass(frozen=True)
class SymbolicFactor:
    n: int
    ordering: OrderingResult
    # Lower factor L stored by columns (CSC), including unit diagonal.
    l_indptr: np.ndarray
    l_indices: np.ndarray
    etree_parent: np.ndarray            # elimination tree, -1 at roots
    col_counts: np.ndarray              # entries per column of L
    nnz_lower: int                      # stored entries incl. diagonal
    nnz_orig_lower: int                 # lower entries of P A P^T
    fill_entries: int                   # nnz_lower - nnz_orig_lower
    pattern_fingerprint: str

    def column_rows(self, k: int) -> np.ndarray:
        """Row indices of nonzeros in column k of L (k itself first)."""
        return self.l_indices[self.l_indptr[k]:self.l_indptr[k + 1]]

    def pattern_signature(self) -> tuple[int, str]:
        return self.n, self.pattern_fingerprint


def _permuted_pattern(upper: sp.csr_matrix, perm: np.ndarray) -> sp.csr_matrix:
    """B = P A P^T as a symmetric sparse matrix (both triangles).

    ``upper`` holds one triangle; we mirror it, then permute. No dense
    intermediate is created.
    """
    sym = upper + upper.T
    # Mirroring doubles the diagonal; subtract one copy.
    sym = sym - sp.diags(np.asarray(upper.diagonal()).ravel())
    sym.eliminate_zeros()
    b = sym[perm, :][:, perm].tocsr()
    b.sort_indices()
    return b


def elimination_tree_from_pattern(columns: list[np.ndarray],
                                  n: int) -> np.ndarray:
    """Column elimination tree: parent[k] = smallest row i > k with
    L[i,k] != 0, i.e. the first sub-diagonal row of column k.

    A column with no sub-diagonal rows is a tree root (-1).
    """
    parent = np.full(n, NO_PARENT, dtype=np.int64)
    for k in range(n):
        rows = columns[k]
        below = rows[rows > k]
        if below.size:
            parent[k] = int(below[0])
    return parent


def cholesky_pattern(b: sp.csr_matrix, n: int):
    """Exact L row structure by incremental left-looking Boolean union.

    Exact fill identity (from the LDL update formula):

        pattern(L[:,k]) = {k} U {i>k : B[i,k] != 0}
                          U U_{j<k, k in pattern(L[:,j])}
                                {i in pattern(L[:,j]) : i > k}

    Every earlier column j that has a nonzero in row k (L[k,j] != 0)
    contributes its rows below k. This is the sparse reach set computed
    directly, so it cannot depend on etree orientation conventions.
    """
    b_csc = b.tocsc()
    columns: list[np.ndarray] = []
    col_counts = np.zeros(n, dtype=np.int64)
    # row_cols[r] = earlier columns j already formed with r in pattern(j)
    row_cols: list[list[int]] = [[] for _ in range(n)]

    for k in range(n):
        idx = b_csc.indices[b_csc.indptr[k]:b_csc.indptr[k + 1]]
        rows = {int(i) for i in idx if i > k}
        for j in row_cols[k]:
            for r in columns[j]:
                if r > k:
                    rows.add(int(r))
        rows.add(k)
        rows_arr = np.sort(np.fromiter(rows, dtype=np.int64,
                                       count=len(rows)))
        columns.append(rows_arr)
        col_counts[k] = rows_arr.size
        for r in rows_arr:
            if r > k:
                row_cols[int(r)].append(k)
    return columns, col_counts


def pattern_hash(upper: sp.csr_matrix) -> str:
    """Hash of the *original* upper-triangle sparsity pattern."""
    coo = upper.tocoo()
    edges = sorted(zip(coo.row.tolist(), coo.col.tolist()))
    h = hashlib.sha256()
    h.update(str(upper.shape[0]).encode())
    for r, c in edges:
        h.update(f"{r},{c};".encode())
    return h.hexdigest()[:16]


def symbolic_factor(upper: sp.csr_matrix,
                    ordering: OrderingResult) -> SymbolicFactor:
    """Run the complete symbolic phase."""
    n = upper.shape[0]
    perm = ordering.perm
    b = _permuted_pattern(upper, perm)

    lower = sp.tril(b, k=-1, format="coo")
    nnz_orig_lower = int(lower.nnz)

    columns, col_counts = cholesky_pattern(b, n)
    parent = elimination_tree_from_pattern(columns, n)

    # Pack columns into flat CSC arrays.
    indptr = np.zeros(n + 1, dtype=np.int64)
    indptr[1:] = np.cumsum(col_counts)
    indices = np.empty(int(indptr[n]), dtype=np.int64)
    for k, col in enumerate(columns):
        indices[indptr[k]:indptr[k + 1]] = col

    nnz_lower = int(indptr[n])
    fp = pattern_hash(upper)

    return SymbolicFactor(
        n=n,
        ordering=ordering,
        l_indptr=indptr,
        l_indices=indices,
        etree_parent=parent,
        col_counts=col_counts,
        nnz_lower=nnz_lower,
        nnz_orig_lower=nnz_orig_lower,
        fill_entries=nnz_lower - (nnz_orig_lower + n),
        pattern_fingerprint=fp,
    )


def pattern_matches(sym: SymbolicFactor, upper: sp.csr_matrix) -> bool:
    """Strict reuse guard: reuse only when patterns are *identical*."""
    n = upper.shape[0]
    if n != sym.n:
        return False
    return pattern_hash(upper) == sym.pattern_fingerprint
