"""Numerical sparse LDL^T factorization and triangular solves.

Given B = P A P^T and a :class:`SymbolicFactor` from the symbolic phase,
compute

    B = L D L^T

with L unit lower triangular, D diagonal. Left-looking sparse-column
scheme: the L values are written into the *exact* positions predicted
symbolically. A single length-n scatter workspace is used (the matrix
itself is never densified).

Pivot failures are reported with the elimination position, the original
matrix index and the offending pivot value, classified as singular or
non-positive-definite.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp

from ..config import settings
from ..errors import ErrorCode, FactorizationError
from .symbolic import SymbolicFactor


@dataclass(frozen=True)
class NumericalFactor:
    symbolic: SymbolicFactor
    # Unit lower factor L in CSC layout (positions match symbolic arrays).
    l_indptr: np.ndarray
    l_indices: np.ndarray
    l_values: np.ndarray
    diag: np.ndarray                     # diagonal of D
    pivot_positions: np.ndarray          # elimination position -> original idx
    elapsed_seconds: float
    n: int

    def as_csc(self) -> sp.csc_matrix:
        return sp.csc_matrix(
            (self.l_values, self.l_indices, self.l_indptr),
            shape=(self.n, self.n))

    def as_lt_csc(self) -> sp.csc_matrix:
        """D^{-1/2}-free lower triangle as L itself (unit diagonal)."""
        return self.as_csc()


def _column_span(sym: SymbolicFactor, j: int):
    return sym.l_indptr[j], sym.l_indptr[j + 1]


def ldlt_factor(b: sp.csr_matrix, sym: SymbolicFactor,
                logger=None) -> NumericalFactor:
    """Left-looking sparse LDL^T numeric factorization.

    Args:
        b: permuted matrix P A P^T (both triangles, CSR).
        sym: symbolic factor computed for the same permutation.
    """
    n = sym.n
    started = time.perf_counter()

    l_values = np.zeros(sym.l_indices.shape[0], dtype=np.float64)
    # Unit diagonal positions: first entry of every column is its pivot row.
    diag = np.zeros(n, dtype=np.float64)
    for k in range(n):
        l_values[sym.l_indptr[k]] = 1.0

    # Offset of a column's pivot row inside that column (= 0 by build).
    b_csc = b.tocsc()
    b_csc.sort_indices()

    # Row history: for each row r, list of (column j, L[r,j]) already formed.
    row_cols: list[list[int]] = [[] for _ in range(n)]
    row_vals: list[list[float]] = [[] for _ in range(n)]

    # Scatter workspace (single dense *vector*, never a dense matrix).
    work = np.zeros(n, dtype=np.float64)
    touched: list[int] = []

    diag_scale = float(np.max(np.abs(b.diagonal())))
    if diag_scale == 0.0:
        diag_scale = 1.0
    floor = settings.pivot_tol * diag_scale

    for k in range(n):
        # --- scatter column k of B (rows i >= k only) ------------------
        start_b = b_csc.indptr[k]
        end_b = b_csc.indptr[k + 1]
        b_idx = b_csc.indices[start_b:end_b]
        b_val = b_csc.data[start_b:end_b]
        ge = b_idx >= k
        for i, v in zip(b_idx[ge], b_val[ge]):
            ii = int(i)
            if work[ii] == 0.0:
                touched.append(ii)
            work[ii] = float(v)

        # --- left-looking updates from prior columns j with L[k,j] != 0
        col_start, col_end = _column_span(sym, k)
        kcol = sym.l_indices[col_start:col_end]
        for j, lkj in zip(row_cols[k], row_vals[k]):
            js, je = _column_span(sym, j)
            jrows = sym.l_indices[js:je]
            jvals = l_values[js:je]
            # Only rows r >= k can affect column k (L is lower triangular).
            p = int(np.searchsorted(jrows, k))
            coeff = lkj * diag[j]
            for r, lrj in zip(jrows[p:], jvals[p:]):
                rr = int(r)
                if work[rr] == 0.0:
                    touched.append(rr)
                work[rr] -= coeff * float(lrj)

        pivot = work[k]
        if not np.isfinite(pivot):
            raise FactorizationError(
                ErrorCode.NON_SPD_PIVOT,
                f"non-finite pivot at elimination position {k}",
                pivot=k, pivot_value=float(pivot))
        if abs(pivot) <= floor:
            orig = int(sym.ordering.perm[k])
            if logger is not None:
                logger.error("pivot_failure", position=k,
                             original_index=orig, pivot_value=float(pivot),
                             kind="singular", threshold=floor)
            raise FactorizationError(
                ErrorCode.SINGULAR_PIVOT,
                f"singular pivot |d_{k}|={abs(pivot):.3e} <= {floor:.3e}",
                pivot=k, original_index=orig, pivot_value=float(pivot),
                details={"threshold": floor})
        if pivot < 0.0:
            orig = int(sym.ordering.perm[k])
            if logger is not None:
                logger.error("pivot_failure", position=k,
                             original_index=orig, pivot_value=float(pivot),
                             kind="negative", threshold=floor)
            raise FactorizationError(
                ErrorCode.NON_SPD_PIVOT,
                f"negative pivot d_{k}={pivot:.3e}; matrix is not SPD",
                pivot=k, original_index=orig, pivot_value=float(pivot),
                details={"threshold": floor})

        diag[k] = pivot

        # --- compute L[r,k] for the symbolically predicted rows --------
        for r in kcol[kcol > k]:
            rr = int(r)
            val = work[rr] / pivot
            # locate position of rr inside column k
            off = int(np.searchsorted(kcol, rr))
            l_values[col_start + off] = val
            row_cols[rr].append(k)
            row_vals[rr].append(val)

        if logger is not None and (k == 0 or (k + 1) % max(1, n // 8) == 0):
            logger.info("factor_progress", column=k + 1, of=n,
                        pivot=float(pivot))

        for rr in touched:
            work[rr] = 0.0
        touched.clear()

    elapsed = time.perf_counter() - started
    return NumericalFactor(
        symbolic=sym,
        l_indptr=sym.l_indptr,
        l_indices=sym.l_indices,
        l_values=l_values,
        diag=diag,
        pivot_positions=sym.ordering.perm,
        elapsed_seconds=elapsed,
        n=n,
    )


def ldlt_solve(fac: NumericalFactor, rhs_perm: np.ndarray) -> np.ndarray:
    """Solve (L D L^T) y = b with b already permuted (b = P rhs)."""
    n = fac.n
    b = np.asarray(rhs_perm, dtype=np.float64).copy()

    # Forward substitution L z = b (unit diagonal), column sweep.
    for j in range(n):
        js, je = fac.l_indptr[j], fac.l_indptr[j + 1]
        rows = fac.l_indices[js:je]
        vals = fac.l_values[js:je]
        bj = b[j]
        if bj != 0.0:
            below = rows > j
            b[rows[below]] -= vals[below] * bj

    # Diagonal solve D y = z.
    b /= fac.diag

    # Back substitution L^T x = y, column sweep in reverse.
    for j in range(n - 1, -1, -1):
        js, je = fac.l_indptr[j], fac.l_indptr[j + 1]
        rows = fac.l_indices[js:je]
        vals = fac.l_values[js:je]
        below = rows > j
        # x_j = y_j - sum_{i>j} L[i,j] x_i
        b[j] -= float(np.dot(vals[below], b[rows[below]]))
    return b


def solve_original(fac: NumericalFactor, rhs: np.ndarray) -> np.ndarray:
    """Full solve with permutation applied to the RHS and undone on x."""
    rhs = np.asarray(rhs, dtype=np.float64)
    if rhs.shape != (fac.n,):
        from ..errors import InputValidationError
        raise InputValidationError(
            ErrorCode.SIZE_MISMATCH,
            f"rhs length {rhs.shape} != matrix dimension {fac.n}")
    perm = fac.symbolic.ordering.perm
    x_perm = ldlt_solve(fac, rhs[perm])
    x = np.empty(fac.n, dtype=np.float64)
    x[perm] = x_perm
    return x
