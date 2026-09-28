"""Error evidence.

Independently measures factorization/solution quality. None of these
routines use the kernel under test to grade itself:

* residual and reconstruction errors are computed straight from the
  sparse matrices;
* the reference solution comes from LAPACK via ``numpy.linalg.solve``
  (a dense, independently implemented path);
* an optional high-precision reference uses *mpmath* arbitrary-precision
  rational/MP arithmetic.

Densification is only used for small reference matrices (bounded size).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp

from ..config import settings
from ..core.numerical import NumericalFactor


@dataclass(frozen=True)
class Evidence:
    residual_abs: float          # ||A x - b||_inf
    residual_rel: float          # / (||A||_inf ||x||_inf + ||b||_inf)
    reconstruction_abs: float    # ||L D L^T - P A P^T||_inf
    dense_solution_error: float  # ||x - x_lapack||_inf (small n)
    mpmath_solution_error: float | None  # ||x - x_mpmath||_inf (small n)
    fill_ratio: float            # nnz(L) / nnz(lower triangle of A)
    passed: bool
    reasons: tuple[str, ...]


def full_symmetric(upper: sp.csr_matrix) -> sp.csr_matrix:
    """Mirror the upper triangle into a full sparse symmetric matrix."""
    a = upper + upper.T
    a = a - sp.diags(np.asarray(upper.diagonal()).ravel())
    a.eliminate_zeros()
    return a.tocsr()


def permute(a: sp.csr_matrix, perm: np.ndarray) -> sp.csr_matrix:
    return a[perm, :][:, perm].tocsr()


def residual_norm(a: sp.csr_matrix, x: np.ndarray,
                  b: np.ndarray) -> tuple[float, float]:
    r = a @ x - b
    abs_err = float(np.linalg.norm(r, ord=np.inf))
    a_norm = float(np.max(np.asarray(abs(a).sum(axis=1)).ravel())) \
        if a.nnz else 0.0
    denom = a_norm * float(np.linalg.norm(x, ord=np.inf)) \
        + float(np.linalg.norm(b, ord=np.inf))
    rel = abs_err / denom if denom > 0 else abs_err
    return abs_err, rel


def reconstruction_error(fac: NumericalFactor,
                         b_mat: sp.csr_matrix) -> float:
    """||L D L^T - B||_inf using only sparse products."""
    l = fac.as_csc().tocsr()                       # unit lower L
    d = sp.diags(fac.diag)
    rebuilt = (l @ d @ l.T).tocsr()
    diff = rebuilt - b_mat
    if diff.nnz == 0:
        return 0.0
    return float(np.max(np.abs(diff.data)))


def dense_reference_solve(upper: sp.csr_matrix,
                          b: np.ndarray) -> np.ndarray:
    """Independent LAPACK reference (dense). Bounded to modest sizes."""
    a_dense = full_symmetric(upper).toarray()
    return np.linalg.solve(a_dense, np.asarray(b, dtype=np.float64))


def mpmath_reference_solve(upper: sp.csr_matrix,
                           b: np.ndarray,
                           dps: int | None = None):
    """Arbitrary-precision Cholesky solve via mpmath.

    Returns None if mpmath is unavailable; raises only for non-SPD input
    at high precision (which is itself evidence).
    """
    import mpmath

    dps = dps or settings.mpmath_dps
    n = upper.shape[0]
    a_mp = mpmath.matrix(n, n)
    a_dense = full_symmetric(upper).toarray()
    for i in range(n):
        for j in range(n):
            a_mp[i, j] = mpmath.mpf(repr(float(a_dense[i, j])))
    b_mp = mpmath.matrix([mpmath.mpf(repr(float(v))) for v in b])
    old_dps = mpmath.mp.dps
    mpmath.mp.dps = dps
    try:
        cho = mpmath.cholesky(a_mp)              # lower-triangular mp matrix
        # L y = b
        y = mpmath.lu_solve(cho, b_mp)
        # L^T x = y
        x = mpmath.lu_solve(cho.T, y)
        return np.asarray([float(v) for v in x], dtype=np.float64)
    finally:
        mpmath.mp.dps = old_dps


MPMATH_MAX_N = 60
DENSE_MAX_N = 2000


def evaluate(fac: NumericalFactor, upper: sp.csr_matrix,
             rhs: np.ndarray, x: np.ndarray,
             *, with_mpmath: bool = True) -> Evidence:
    """Collect the full evidence bundle for a completed solve."""
    n = fac.n
    perm = fac.symbolic.ordering.perm
    a = full_symmetric(upper)
    b_mat = permute(a, perm)

    reasons: list[str] = []
    eps = float(np.finfo(np.float64).eps)

    res_abs, res_rel = residual_norm(a, x, rhs)
    if not np.isfinite(res_abs) or res_rel > settings.residual_warn_factor:
        reasons.append(
            f"relative residual {res_rel:.3e} exceeds "
            f"{settings.residual_warn_factor:.0e} (eps={eps:.2e})")

    recon = reconstruction_error(fac, b_mat)
    scale_b = float(np.max(np.abs(b_mat.data))) if b_mat.nnz else 1.0
    if not np.isfinite(recon) or recon > 1e-8 * scale_b:
        reasons.append(
            f"reconstruction error {recon:.3e} exceeds 1e-8*||B||")

    dense_err: float | None = None
    if n <= DENSE_MAX_N:
        x_ref = dense_reference_solve(upper, rhs)
        dense_err = float(np.max(np.abs(x - x_ref)))
        if not np.isfinite(dense_err) or dense_err > 1e-7 * max(
                1.0, float(np.max(np.abs(x_ref)))):
            reasons.append(
                f"solution differs from LAPACK by {dense_err:.3e}")

    mp_err: float | None = None
    if with_mpmath and n <= MPMATH_MAX_N:
        x_mp = mpmath_reference_solve(upper, rhs)
        mp_err = float(np.max(np.abs(x - x_mp)))
        if not np.isfinite(mp_err) or mp_err > 1e-6 * max(
                1.0, float(np.max(np.abs(x_mp)))):
            reasons.append(
                f"solution differs from mpmath({settings.mpmath_dps}dps) "
                f"by {mp_err:.3e}")

    orig_lower = fac.symbolic.nnz_orig_lower + n
    fill_ratio = fac.symbolic.nnz_lower / max(1, orig_lower)

    return Evidence(
        residual_abs=res_abs,
        residual_rel=res_rel,
        reconstruction_abs=recon,
        dense_solution_error=dense_err if dense_err is not None
        else float("nan"),
        mpmath_solution_error=mp_err,
        fill_ratio=fill_ratio,
        passed=not reasons,
        reasons=tuple(reasons),
    )
