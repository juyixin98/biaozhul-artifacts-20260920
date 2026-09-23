"""Shared helpers for the test suite."""

from __future__ import annotations

import numpy as np

from sparse_cg.csr import CSRMatrix


def dense_to_csr(D: np.ndarray) -> CSRMatrix:
    """Build a CSRMatrix from a dense 2-D array (test helper)."""
    D = np.asarray(D, dtype=np.float64)
    n = D.shape[0]
    rows, cols, vals = [], [], []
    for i in range(n):
        for j in range(n):
            if D[i, j] != 0.0:
                rows.append(i)
                cols.append(j)
                vals.append(float(D[i, j]))
    return CSRMatrix.from_triplets(n, rows, cols, vals)


def true_residual_norm(A: CSRMatrix, x: np.ndarray, b: np.ndarray) -> float:
    """The honest acceptance quantity: ||b - Ax||_2."""
    return float(np.linalg.norm(b - A.matvec(x)))


def relative_true_residual(A, x, b):
    nb = float(np.linalg.norm(b))
    rn = true_residual_norm(A, x, b)
    return rn / nb if nb > 0.0 else rn
