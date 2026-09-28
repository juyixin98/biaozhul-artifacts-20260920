"""Synthetic local fixtures and *independent* reference solutions.

Reference answers are never produced by the solver under test:

* :func:`reference_solve_mp` uses mpmath's own dense ``lu_solve`` at 150 dps -
  a completely different code path from the staged engine.
* :func:`reference_solve_fraction` uses exact rational Gaussian elimination
  (``fractions.Fraction``), which is exact for integer/rational inputs.

Condition-controlled matrices come from A = Q1 diag(s) Q2 with Haar-like
orthogonal factors built by QR of seeded random matrices, so the singular
values are known exactly and the ill-conditioning is genuine rather than the
artifact of a hand-built 2x2.
"""

from __future__ import annotations

import random
from fractions import Fraction

import mpmath
import numpy as np
import scipy.linalg as sla

from app.numerical import mpf, workprec

REFERENCE_DPS = 150


def seeded_random(seed: int) -> random.Random:
    return random.Random(seed)


def random_orthogonal(n: int, rng: random.Random, dps: int = 80):
    with workprec(dps):
        # mpmath.matrix(rows, cols, flat_list) drops the data; pass rows.
        raw = mpmath.matrix(
            [[mpf(rng.gauss(0.0, 1.0)) for _ in range(n)] for _ in range(n)]
        )
        q, _ = mpmath.qr(raw)
        return q


def conditioned_matrix(n: int, log10_cond: float, seed: int = 7, dps: int = 80):
    """Dense n x n matrix with geometric singular values in [10^-lc, 1]."""
    rng = seeded_random(seed)
    with workprec(dps):
        q1 = random_orthogonal(n, rng, dps)
        q2 = random_orthogonal(n, seeded_random(seed + 100), dps)
        if n == 1:
            singulars = [mpf(1)]
        else:
            singulars = [
                mpf(10) ** (-mpf(log10_cond) * mpf(k) / mpf(n - 1))
                for k in range(n)
            ]
        d = mpmath.diag(singulars)
        a = q1 * d * q2
        x_true = mpmath.matrix(
            [[mpf(rng.gauss(0.0, 1.0))] for _ in range(n)]
        )
        b = a * x_true
        return a, b, x_true, singulars


def well_conditioned_matrix(n: int, seed: int = 11, dps: int = 80):
    rng = seeded_random(seed)
    with workprec(dps):
        q1 = random_orthogonal(n, rng, dps)
        q2 = random_orthogonal(n, seeded_random(seed + 1), dps)
        singulars = [mpf(1) + mpf(k) * mpf("0.37") for k in range(n)]
        a = q1 * mpmath.diag(singulars) * q2
        x_true = mpmath.matrix(
            [[mpf(rng.gauss(0.0, 1.0))] for _ in range(n)]
        )
        b = a * x_true
        return a, b, x_true


def directional_rhs_system(n: int, log10_cond: float, seed: int = 42, dps: int = 80):
    """A with two RHS columns aligned to the largest and smallest singular vectors.

    Returns ``(a, B, singular_values)`` where B[:,0] is along the largest left
    singular vector and B[:,1] along the smallest. At a fixed iteration budget
    of low-precision refinement these columns converge at measurably different
    rates, which is what the per-column reporting must distinguish.
    """
    with workprec(dps):
        q1 = random_orthogonal(n, seeded_random(seed), dps)
        q2 = random_orthogonal(n, seeded_random(seed + 100), dps)
        singulars = [
            mpf(10) ** (-mpf(log10_cond) * mpf(k) / mpf(n - 1))
            for k in range(n)
        ]
        a = q1 * mpmath.diag(singulars) * q2
        big = q1 * mpmath.matrix(
            [[mpf(1)] if i == 0 else [mpf(0)] for i in range(n)]
        )
        small = q1 * mpmath.matrix(
            [[mpf(1)] if i == n - 1 else [mpf(0)] for i in range(n)]
        )
        b = mpmath.matrix(n, 2)
        for i in range(n):
            b[i, 0] = big[i, 0]
            b[i, 1] = small[i, 0]
    return a, b, singulars


def exact_singular_matrix(seed: int = 3, dps: int = 80):
    """Rank-deficient integer matrix with a known linear dependence."""
    # Rows: r3 = 2*r1 - r2  -> exact rank deficiency, integer entries.
    rows = [
        [mpf(2), mpf(1), mpf(-1)],
        [mpf(1), mpf(-3), mpf(2)],
        [mpf(3), mpf(5), mpf(-4)],  # 2*row1 - row2
    ]
    with workprec(dps):
        a = mpmath.matrix(rows)
        # A right-hand side that WOULD be requested for a unique solve.
        b = mpmath.matrix([mpf(1), mpf(2), mpf(0)])
    return a, b


def reference_solve_mp(a, b, dps: int = REFERENCE_DPS):
    """Independent high-precision reference: mpmath's built-in dense LU."""
    with workprec(dps):
        return mpmath.lu_solve(a, b)


def reference_solve_fraction(a_rows, b_rows):
    """Exact rational Gaussian elimination (independent of all float code)."""
    n = len(a_rows)
    mat = [
        [Fraction(str(v)) for v in row] + [Fraction(str(b_rows[i]))]
        for i, row in enumerate(a_rows)
    ]
    for k in range(n):
        pivot = max(range(k, n), key=lambda i: abs(mat[i][k]))
        mat[k], mat[pivot] = mat[pivot], mat[k]
        for i in range(k + 1, n):
            if mat[i][k] != 0:
                factor = mat[i][k] / mat[k][k]
                for j in range(k, n + 1):
                    mat[i][j] -= factor * mat[k][j]
    x = [Fraction(0)] * n
    for i in range(n - 1, -1, -1):
        x[i] = (mat[i][n] - sum(mat[i][j] * x[j] for j in range(i + 1, n))) / mat[i][i]
    return x


def direct_low_precision_solve(a, b, dtype_name: str):
    """What a naive direct solve at float32/float64 gives (no refinement)."""
    dtype = np.dtype(dtype_name)
    n = a.rows
    a_np = np.array([[float(a[i, j]) for j in range(n)] for i in range(n)], dtype=dtype)
    nrhs = b.cols
    b_np = np.array(
        [[float(b[i, j]) for j in range(nrhs)] for i in range(n)], dtype=dtype
    )
    lu, piv = sla.lu_factor(a_np)
    x = sla.lu_solve((lu, piv), b_np)
    return x


def relative_forward_error(x_computed, x_reference) -> float:
    """||x - xref||_inf / ||xref||_inf evaluated in float of the reference."""
    with workprec(REFERENCE_DPS):
        if hasattr(x_computed, "tolist"):
            diffs = [
                abs(mpf(float(x_computed[i, 0])) - x_reference[i, 0])
                for i in range(x_reference.rows)
            ]
        else:
            diffs = [
                abs(x_computed[i, 0] - x_reference[i, 0])
                for i in range(x_reference.rows)
            ]
        ref = max(abs(x_reference[i, 0]) for i in range(x_reference.rows))
        return float(max(diffs) / ref)
