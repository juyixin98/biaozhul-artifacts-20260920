"""Numeric sparse Cholesky-style factorization.

The backend computes the factorization

    A = L D L^T

where ``L`` is unit lower triangular with the structure predicted by the
symbolic phase and ``D`` is diagonal.  LDL^T avoids square roots and keeps a
clean separation between structural nonzeros (``L``) and pivot values
(``D``); a conventional Cholesky factor ``L_c = L sqrt(D)`` is available on
demand.

The kernel is **left-looking**: column ``k`` is formed by gathering column
``k`` of ``A`` and subtracting, for every earlier column ``j`` known to reach
``k``, the rank-one term ``d_j * L[:,j] * L[k,j]``.  The set of such ``j``
and the rows touched are taken verbatim from :mod:`symbolic`, so numeric work
never touches a structural zero and the matrix is never densified.

Positive-definiteness is determined pivot by pivot: a non-positive pivot is a
proof that the matrix is not positive definite, and the offending index is
reported in both the permuted and original numbering.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
from scipy import sparse

from config.settings import FactorizationConfig
from sparse_cholesky.input.errors import (
    NonPositiveDefiniteError,
    PivotTooSmallError,
)

from .symbolic import SymbolicFactor

#: Callback for progress reporting: (step, k, n, pivot_value).
ProgressHook = Callable[[str, int, int, float], None]


@dataclass
class NumericFactor:
    """Result of the numeric factorization.

    ``l_values`` is packed exactly like ``symbolic.row_idx``: one entry per
    predicted nonzero, with diagonal slots equal to ``1.0``.  This keeps the
    addressing identical to the symbolic structure (``col_ptr`` + local
    position) instead of maintaining two offset schemes.
    """

    symbolic: SymbolicFactor
    l_values: np.ndarray
    diag: np.ndarray
    #: Original (pre-update) diagonal value a[k, k] for each column.
    input_diag: np.ndarray
    config: FactorizationConfig

    @property
    def n(self) -> int:
        return self.symbolic.n

    def l_csc(self) -> sparse.csc_matrix:
        """Materialize the unit-lower factor L as a sparse CSC matrix.

        The packed storage is already CSC form for the lower factor:
        ``row_idx`` are row indices and ``col_ptr`` are column pointers.
        """
        sym = self.symbolic
        return sparse.csc_matrix(
            (self.l_values.copy(), sym.row_idx, sym.col_ptr),
            shape=(sym.n, sym.n),
        )

    def cholesky_csc(self) -> sparse.csc_matrix:
        """Conventional Cholesky factor ``L_c`` with ``A = L_c L_c^T``."""
        unit = self.l_csc()
        sqrt_d = np.sqrt(self.diag)
        # Column scaling: L_c[:, k] = sqrt(d_k) * L[:, k].
        return unit @ sparse.diags(sqrt_d, format="csc")


def numeric_factorization(
    matrix_csc: sparse.csc_matrix,
    symbolic: SymbolicFactor,
    config: FactorizationConfig,
    *,
    progress: ProgressHook | None = None,
) -> NumericFactor:
    """Factor ``matrix_csc`` along the given symbolic structure.

    Raises
    ------
    NonPositiveDefiniteError
        A pivot ``d[k] <= 0`` (or non-finite) is encountered.
    PivotTooSmallError
        A positive pivot falls below the configured reliability threshold.
    """
    n = symbolic.n
    if matrix_csc.shape != (n, n):
        raise ValueError(
            f"matrix shape {matrix_csc.shape} does not match symbolic n={n}"
        )

    indptr, indices, mdata = matrix_csc.indptr, matrix_csc.indices, matrix_csc.data
    col_rows = symbolic.col_rows
    row_cols = symbolic.row_cols
    col_ptr = symbolic.col_ptr

    diag = np.empty(n, dtype=np.float64)
    input_diag = np.empty(n, dtype=np.float64)
    l_values = np.ones(symbolic.nnz_lower, dtype=np.float64)  # diag slots = 1

    # Sparse accumulator for the current column.
    work: dict[int, float] = {}

    for k in range(n):
        work.clear()

        # Gather A[i, k] for i >= k.
        for p in range(indptr[k], indptr[k + 1]):
            r = int(indices[p])
            if r >= k:
                work[r] = float(mdata[p])
        a_kk = work.get(k, 0.0)
        input_diag[k] = a_kk

        # Subtract earlier-column contributions: d_j * L[:,j] * L[k,j].
        start_k = int(col_ptr[k])
        for j in row_cols[k]:
            rows_j = col_rows[j]
            start_j = int(col_ptr[j])
            pos_k = int(np.searchsorted(rows_j, k))
            l_kj = l_values[start_j + pos_k]
            factor = diag[j] * l_kj
            # Pivot self-update: d_k = a_kk - sum_j d_j L[k,j]^2.
            work[k] = work.get(k, 0.0) - l_kj * factor
            # Off-diagonal update for rows structurally below k.
            for t in range(pos_k + 1, rows_j.size):
                r = int(rows_j[t])
                work[r] = work.get(r, 0.0) - l_values[start_j + t] * factor

        pivot = work.get(k, 0.0)
        _check_pivot(pivot, a_kk, k, config)
        diag[k] = pivot
        if progress is not None:
            progress("pivot", k, n, pivot)

        # L[i, k] = work[i] / d[k] for every predicted sub-diagonal row.
        rows_k = col_rows[k]
        for t in range(1, rows_k.size):  # t=0 is the diagonal (= k)
            r = int(rows_k[t])
            l_values[start_k + t] = work.get(r, 0.0) / pivot

    return NumericFactor(
        symbolic=symbolic,
        l_values=l_values,
        diag=diag,
        input_diag=input_diag,
        config=config,
    )


def _check_pivot(pivot: float, a_kk: float, k: int,
                 config: FactorizationConfig) -> None:
    if not np.isfinite(pivot):
        raise NonPositiveDefiniteError(
            f"pivot {k} is not finite ({pivot}); factorization cannot continue",
            pivot_index=k,
            pivot_value=pivot,
        )
    if pivot <= 0.0:
        raise NonPositiveDefiniteError(
            f"matrix is not positive definite: pivot {k} = {pivot:.6e} <= 0",
            pivot_index=k,
            pivot_value=pivot,
        )
    threshold = max(
        config.pivot_tol_abs,
        config.pivot_tol_rel * abs(a_kk),
    )
    if pivot < threshold:
        raise PivotTooSmallError(
            f"pivot {k} = {pivot:.6e} is below reliability threshold "
            f"{threshold:.6e}",
            pivot_index=k,
            pivot_value=pivot,
        )
