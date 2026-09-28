"""High-precision error evidence via mpmath.

The production kernels run in float64.  To produce *independent* evidence of
their accuracy (rather than trusting a second float64 computation), this
module re-solves small systems in arbitrary precision using a dense LDL^T
factorization written directly on top of :mod:`mpmath`.

It is deliberately bounded in size: high-precision dense arithmetic is a
verification instrument for small fixtures, not a production path.  The
sparse production kernels never call this module and never densify.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import mpmath

#: Largest order for which high-precision dense evidence is permitted.
MAX_HP_ORDER = 200


class HighPrecisionTooLargeError(ValueError):
    """Raised when requesting high-precision evidence for a large matrix."""


@dataclass(frozen=True)
class HighPrecisionEvidence:
    dps: int
    forward_error_inf: float
    x_norm_inf: float
    reference_residual_inf: float
    x_excerpt: tuple[str, ...]


def _to_mpmatrix(a: np.ndarray, dps: int) -> mpmath.matrix:
    mpmath.mp.dps = dps
    n, m = a.shape
    mat = mpmath.zeros(n, m)
    for i in range(n):
        for j in range(m):
            mat[i, j] = mpmath.mpf(float(a[i, j]))
    return mat


def high_precision_ldlt_solve(a_dense: np.ndarray, b: np.ndarray,
                              *, dps: int = 50) -> np.ndarray:
    """Solve ``A x = b`` with an independent dense mpmath LDL^T.

    Used as an accuracy oracle; inputs must be small (see
    :data:`MAX_HP_ORDER`).
    """
    x_mp = _high_precision_solve_mp(a_dense, b, dps=dps)
    return np.array([float(v) for v in x_mp], dtype=np.float64)


def _high_precision_solve_mp(a_dense: np.ndarray, b: np.ndarray, *,
                             dps: int = 50) -> list:
    """Return the high-precision solution as a list of mpmath numbers."""
    n = a_dense.shape[0]
    if n > MAX_HP_ORDER:
        raise HighPrecisionTooLargeError(
            f"high-precision evidence supports n <= {MAX_HP_ORDER}, got {n}"
        )
    mpmath.mp.dps = dps
    a = _to_mpmatrix(np.asarray(a_dense, dtype=np.float64), dps)
    rhs = _to_mpmatrix(np.asarray(b, dtype=np.float64).reshape(-1, 1), dps)

    # In-place LDL^T (L unit lower, D diagonal).
    d = [mpmath.mpf(0)] * n
    for k in range(n):
        acc = a[k, k]
        for j in range(k):
            acc -= d[j] * a[k, j] * a[k, j]
        if acc <= 0:
            raise ValueError(
                f"high-precision reference encountered non-positive pivot {k}"
            )
        d[k] = acc
        for i in range(k + 1, n):
            acc = a[i, k]
            for j in range(k):
                acc -= d[j] * a[i, j] * a[k, j]
            a[i, k] = acc / d[k]

    # Forward solve L y = b.
    y = [mpmath.mpf(0)] * n
    for i in range(n):
        acc = rhs[i, 0]
        for j in range(i):
            acc -= a[i, j] * y[j]
        y[i] = acc
    # Diagonal solve and back substitution L^T x = z.
    z = [y[i] / d[i] for i in range(n)]
    x = [mpmath.mpf(0)] * n
    for i in range(n - 1, -1, -1):
        acc = z[i]
        for r in range(i + 1, n):
            acc -= a[r, i] * x[r]
        x[i] = acc
    return x


def high_precision_evidence(a_dense: np.ndarray, b: np.ndarray,
                            x_float64: np.ndarray, *,
                            dps: int = 50,
                            excerpt: int = 5) -> HighPrecisionEvidence:
    """Compare a float64 solution against the high-precision reference."""
    mpmath.mp.dps = dps
    x_mp = _high_precision_solve_mp(a_dense, b, dps=dps)
    x_ref = np.array([float(v) for v in x_mp], dtype=np.float64)
    err = np.asarray(x_float64, dtype=np.float64) - x_ref
    fwd = float(np.linalg.norm(err, ord=np.inf))
    xnorm = float(np.linalg.norm(x_ref, ord=np.inf))

    # Residual evaluated entirely in high precision (not via float64 arrays).
    a_mp = _to_mpmatrix(np.asarray(a_dense, dtype=np.float64), dps)
    res_max = mpmath.mpf(0)
    for i in range(len(x_mp)):
        acc = mpmath.mpf(0)
        for j in range(len(x_mp)):
            acc += a_mp[i, j] * x_mp[j]
        res_i = abs(acc - mpmath.mpf(float(b[i])))
        if res_i > res_max:
            res_max = res_i

    k = min(excerpt, x_ref.size)
    return HighPrecisionEvidence(
        dps=dps,
        forward_error_inf=fwd / xnorm if xnorm > 0 else fwd,
        x_norm_inf=xnorm,
        reference_residual_inf=float(res_max),
        x_excerpt=tuple(mpmath.nstr(v, 14) for v in x_mp[:k]),
    )
