"""Linear-algebra building blocks.

Projection notation:
    P_A = A (A'A)^-1 A'      (orthogonal projection onto span(A))
    M_A = I - P_A            (annihilator)

Everything uses explicit symmetric pseudo-inverses via eigendecomposition so
that near-singular designs degrade to a *diagnosed* condition instead of
silently producing garbage. ``solve_spd`` raises on non-positive-definiteness
beyond a declared tolerance.
"""
from __future__ import annotations

import numpy as np

from .errors import DegenerateDataError


class LinearAlgebraError(ValueError):
    """Raised internally; converted to DegenerateDataError at service edges."""


def as_matrix(a: np.ndarray, ndim: int = 2) -> np.ndarray:
    arr = np.asarray(a, dtype=float)
    if arr.ndim != ndim:
        raise LinearAlgebraError(f"expected {ndim}D array, got shape {arr.shape}")
    return arr


def pinv_sym(a: np.ndarray, rcond: float | None = None) -> np.ndarray:
    """Inverse/pseudoinverse of a symmetric matrix via its eigendecomposition.

    Eigenvalues with magnitude <= rcond * max|eig| are treated as zero.
    """
    a = as_matrix(a)
    w, v = np.linalg.eigh(a)
    threshold = (rcond if rcond is not None else max(a.shape) * np.finfo(float).eps) * float(
        np.max(np.abs(w), initial=0.0)
    )
    cutoff = max(threshold, np.finfo(float).eps)
    inv_w = np.zeros_like(w)
    nonzero = np.abs(w) > cutoff
    inv_w[nonzero] = 1.0 / w[nonzero]
    return (v * inv_w) @ v.T


def solve_spd(a: np.ndarray, b: np.ndarray, request_id: str, what: str) -> np.ndarray:
    """Solve A x = b requiring A symmetric positive definite.

    Used for variance matrices. A negative eigenvalue is a real failure
    (not just precision), reported as degenerate data.
    """
    w, v = np.linalg.eigh(a)
    eps = max(a.shape) * np.finfo(float).eps * float(np.max(np.abs(w), initial=0.0))
    n_zero = int(np.sum(w <= eps))
    if n_zero:
        raise DegenerateDataError(
            f"{what} is singular: {n_zero} non-positive eigenvalue(s)",
            request_id=request_id,
            key_state={"component": what, "small_eigenvalues": n_zero, "min_eig": float(w[0])},
        )
    return (v * (1.0 / w)) @ v.T @ b


def projection(a: np.ndarray) -> np.ndarray:
    """P_A (n x n). Caller is responsible for A having full column rank."""
    a = as_matrix(a)
    ata = a.T @ a
    return a @ pinv_sym(ata) @ a.T


def annihilator(a: np.ndarray) -> np.ndarray:
    n = a.shape[0]
    return np.eye(n) - projection(a)


def residualize(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """M_A b  (= b - P_A b). Works for b with one or more columns."""
    pa = projection(a)
    return b - pa @ b


def ols_fit(y: np.ndarray, W: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Plain OLS. Returns (coefs, residuals, y_hat)."""
    coefs = pinv_sym(W.T @ W) @ W.T @ y
    fitted = W @ coefs
    resid = y - fitted
    return coefs, resid, fitted


def numerical_rank(a: np.ndarray, tol: float) -> int:
    """Matrix rank with an explicit relative singular-value tolerance."""
    s = np.linalg.svd(a, compute_uv=False)
    if s.size == 0:
        return 0
    return int(np.sum(s > tol * float(s[0])))


def matrix_rank_detail(a: np.ndarray, tol: float) -> tuple[int, np.ndarray]:
    s = np.linalg.svd(a, compute_uv=False)
    if s.size == 0:
        return 0, s
    return int(np.sum(s > tol * float(s[0]))), s
