"""Condition-number estimation.

``cond_1(A) = ||A||_1 * ||A^{-1}||_1`` with the Hager/Higham 1-norm inverse
estimator (the algorithm behind LAPACK ``xLACON``): only solves with A and
A^T are needed, the inverse is never formed. For small matrices a
high-precision SVD supplies the exact 2-norm condition number as independent
corroboration (mpmath returns singular values unordered, so we sort them).
"""

from __future__ import annotations

from dataclasses import dataclass

from mpmath import mpf

from . import mp, workprec
from . import arithmetic as arith

_ESTIMATOR_ITERATIONS = 5


@dataclass(frozen=True)
class ConditionReport:
    method: str
    cond: mpf
    cond_dps: int
    log10_cond: float
    svd_cond: mpf | None = None

    def digits_lost_estimate(self) -> float:
        """Approx decimal digits lost to roundoff in a stable solve (log10 cond)."""
        return self.log10_cond


class _LUHandle:
    """Adapts an MPLU factorization to the two solves the estimator needs."""

    def __init__(self, lu) -> None:
        self._lu = lu
        self.n = lu.n

    def solve(self, column: list[mpf]) -> list[mpf]:
        return self._lu.solve_column(column)

    def solve_transpose(self, column: list[mpf]) -> list[mpf]:
        return self._lu.solve_transpose_column(column)


def estimate_norm_inv_one(handle: _LUHandle) -> mpf:
    """Hager/Higham 1-norm estimate of ||A^{-1}||_1 (xLACON main loop)."""
    n = handle.n
    x = [mpf(1) / mpf(n)] * n
    v = handle.solve(x)
    gamma = sum(abs(w) for w in v)

    prev_j = -1
    for _ in range(_ESTIMATOR_ITERATIONS):
        xi = [mpf(1) if w >= 0 else mpf(-1) for w in v]
        z = handle.solve_transpose(xi)
        j = max(range(n), key=lambda i: abs(z[i]))
        if j == prev_j:
            break
        prev_j = j
        e_j = [mpf(0)] * n
        e_j[j] = mpf(1)
        v = handle.solve(e_j)
        gamma_new = sum(abs(w) for w in v)
        if gamma_new <= gamma:
            break
        gamma = gamma_new
    return gamma


def svd_condition(a, dps: int) -> mpf | None:
    """Exact 2-norm condition via high-precision SVD; mpmath values unsorted."""
    with workprec(dps):
        try:
            singulars = list(mp.svd(a, compute_uv=False))
        except Exception:
            return None
        if not singulars:
            return None
        values = sorted(abs(s) for s in singulars)
        s_min, s_max = values[0], values[-1]
        if s_min == 0:
            return mpf("inf")
        return s_max / s_min


def estimate_condition(a, lu, dps: int, svd_max_n: int) -> ConditionReport:
    n = a.rows
    with workprec(dps):
        norm_a = arith.one_norm(a)
        handle = _LUHandle(lu)
        norm_inv = estimate_norm_inv_one(handle)
        cond = norm_a * norm_inv
        if cond == mpf("inf") or cond <= 0:
            log10 = float("inf")
        else:
            log10 = float(mp.log10(cond))
        svd_cond = None
        method = "1norm-estimate"
        if n <= svd_max_n:
            svd_cond = svd_condition(a, dps)
            if svd_cond is not None:
                method = "1norm-estimate+svd2"
        return ConditionReport(
            method=method,
            cond=cond,
            cond_dps=dps,
            log10_cond=log10,
            svd_cond=svd_cond,
        )
