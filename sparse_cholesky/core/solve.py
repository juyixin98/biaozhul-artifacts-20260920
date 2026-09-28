"""Sparse triangular solves for ``L D L^T`` systems.

Given :class:`NumericFactor` for ``A = L D L^T``:

1. forward substitution  ``L y = b``   (unit diagonal),
2. diagonal scaling      ``D z = y``,
3. back substitution     ``L^T x = z`` (unit diagonal).

Only entries predicted by the symbolic structure are visited.
"""
from __future__ import annotations

import numpy as np

from .numeric import NumericFactor


def forward_substitution(factor: NumericFactor, b: np.ndarray) -> np.ndarray:
    """Solve ``L y = b`` where L is unit lower triangular."""
    sym = factor.symbolic
    n = sym.n
    y = np.array(b, dtype=np.float64, copy=True)
    if y.shape != (n,):
        raise ValueError(f"RHS has shape {y.shape}, expected ({n},)")

    col_rows, row_cols, col_ptr = sym.col_rows, sym.row_cols, sym.col_ptr
    l_values = factor.l_values

    for j in range(n):
        ljj = y[j]  # unit diagonal: no division
        start_j = int(col_ptr[j])
        rows_j = col_rows[j]
        # t=0 is the diagonal slot; sub-diagonal rows follow.
        for t in range(1, rows_j.size):
            r = int(rows_j[t])
            y[r] -= l_values[start_j + t] * ljj
    return y


def diagonal_solve(factor: NumericFactor, y: np.ndarray) -> np.ndarray:
    """Solve ``D z = y`` (pointwise reciprocal)."""
    return y / factor.diag


def back_substitution(factor: NumericFactor, z: np.ndarray) -> np.ndarray:
    """Solve ``L^T x = z`` where L is unit lower triangular.

    Row ``i`` of ``L^T x = z`` is
    ``x_i + sum_{r>i, L[r,i]!=0} L[r,i] x_r = z_i``; walking columns
    backwards, every required ``x_r`` (``r > i``) is already known.
    """
    sym = factor.symbolic
    n = sym.n
    x = np.array(z, dtype=np.float64, copy=True)
    if x.shape != (n,):
        raise ValueError(f"RHS has shape {x.shape}, expected ({n},)")

    col_rows, col_ptr = sym.col_rows, sym.col_ptr
    l_values = factor.l_values

    for i in range(n - 1, -1, -1):
        val = x[i]
        start_i = int(col_ptr[i])
        rows_i = col_rows[i]
        # Local position 0 is the diagonal slot; rows below start at 1.
        for t in range(1, rows_i.size):
            r = int(rows_i[t])
            val -= l_values[start_i + t] * x[r]
        x[i] = val  # unit diagonal
    return x


def solve_ldlt(factor: NumericFactor, b: np.ndarray) -> np.ndarray:
    """Solve ``A x = b`` using the precomputed ``L D L^T`` factorization."""
    y = forward_substitution(factor, b)
    z = diagonal_solve(factor, y)
    return back_substitution(factor, z)
