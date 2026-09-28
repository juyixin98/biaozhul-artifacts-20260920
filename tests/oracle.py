"""Independent reference oracle for tests.

This is deliberately a *separate implementation* from the production kernel:

* :func:`rref_fraction` is textbook augmented Gauss-Jordan written directly on
  :class:`fractions.Fraction` with no Bareiss step, no content reduction and no
  witness bookkeeping - if the production kernel and this code agree across
  thousands of random systems, agreement is not circular.
* :func:`det_fraction` is fraction Gaussian elimination with partial row
  pivoting - an independent determinant path.
* :func:`mpmath_solve` solves at high precision with mpmath (third-party exact
  high precision) and reports residuals.
* :func:`numpy_float64_rank` is the deliberately-imprecise double-precision
  view used to prove near-indistinguishable matrices.

Tests import these rather than deriving expectations from production code.
"""
from __future__ import annotations

from fractions import Fraction

import mpmath
import numpy as np


def rref_fraction(
    A: list[list[Fraction]], b: list[list[Fraction]]
) -> tuple[list[list[Fraction]], list[tuple[int, int]]]:
    """Naive Gauss-Jordan on the augmented matrix.  Returns (RREF, pivots)."""
    m, n = len(A), len(A[0])
    k = len(b[0])
    M = [
        [A[i][j] for j in range(n)] + [b[i][r] for r in range(k)]
        for i in range(m)
    ]
    frontier = 0
    pivots: list[tuple[int, int]] = []
    for col in range(n):
        cand = next(
            (i for i in range(frontier, m) if M[i][col] != 0), None
        )
        if cand is None:
            continue
        if cand != frontier:
            M[cand], M[frontier] = M[frontier], M[cand]
        pv = M[frontier][col]
        M[frontier] = [x / pv for x in M[frontier]]
        for i in range(m):
            if i == frontier or M[i][col] == 0:
                continue
            f = M[i][col]
            M[i] = [M[i][j] - f * M[frontier][j] for j in range(n + k)]
        pivots.append((frontier, col))
        frontier += 1
    return M, pivots


def classify_rhs(
    A: list[list[Fraction]], b_col: list[Fraction]
) -> tuple[str, list[Fraction] | None, list[Fraction] | None]:
    """Return ("unique"|"infinite"|"inconsistent", particular, witness_or_None).

    For inconsistent systems the witness is any y with y.A == 0, y.b != 0,
    read directly out of the oracle's zero rows (independent derivation).
    """
    m, n = len(A), len(A[0])
    b = [[v] for v in b_col]
    M, pivots = rref_fraction(A, b)
    pivot_rows = {r for r, _ in pivots}
    for i in range(m):
        if i in pivot_rows:
            continue
        if M[i][n] != 0:
            # The normalized row [0..0 | c] came from a linear combination of
            # original rows equal to that row; independently *reconstruct* a
            # witness by solving W^T style: simplest is to record the row ops,
            # which the oracle does not do, so instead derive a left-null
            # vector by fraction Gaussian elimination on A^T augmented with b.
            y = _left_null_witness(A, b_col)
            return "inconsistent", None, y
    free = [c for c in range(n) if c not in {pc for _, pc in pivots}]
    x0 = [Fraction(0) for _ in range(n)]
    for r, c in pivots:
        x0[c] = M[r][n]
    return ("unique" if not free else "infinite"), x0, None


def _left_null_witness(
    A: list[list[Fraction]], b_col: list[Fraction]
) -> list[Fraction]:
    """Find y independently: solve A^T z = 0 with the extra constraint dot...
    Practically: augment A^T by -b and run elimination; a null vector of A^T
    whose b-dot is non-zero proves inconsistency.  Built with fresh Fraction
    Gaussian elimination over the *transposed* system.
    """
    m, n = len(A), len(A[0])
    # Null space of A^T: columns are m variables; solve A^T y = 0, then pick
    # the vector with y.b != 0.  Gauss-Jordan of [A^T] (m unknowns, n eqs).
    M = [
        [A[i][j] for i in range(m)]  # row j of A^T = column j of A
        for j in range(n)
    ]
    frontier = 0
    pivot_cols: list[int] = []
    for col in range(m):
        cand = next(
            (r for r in range(frontier, n) if M[r][col] != 0), None
        )
        if cand is None:
            continue
        M[cand], M[frontier] = M[frontier], M[cand]
        pv = M[frontier][col]
        M[frontier] = [x / pv for x in M[frontier]]
        for r in range(n):
            if r != frontier and M[r][col] != 0:
                f = M[r][col]
                M[r] = [M[r][c] - f * M[frontier][c] for c in range(m)]
        pivot_cols.append(col)
        frontier += 1
    free_cols = [c for c in range(m) if c not in pivot_cols]
    for fc in free_cols:
        y = [Fraction(0) for _ in range(m)]
        y[fc] = Fraction(1)
        for r, pc in enumerate(pivot_cols):
            y[pc] = -M[r][fc]
        ytb = sum((y[i] * b_col[i] for i in range(m)), Fraction(0))
        if ytb != 0:
            return y
    raise AssertionError("oracle could not build an inconsistency witness")


def det_fraction(A: list[list[Fraction]]) -> Fraction:
    """Determinant by fraction Gaussian elimination (independent of Bareiss)."""
    n = len(A)
    M = [row[:] for row in A]
    det = Fraction(1)
    for c in range(n):
        cand = next((i for i in range(c, n) if M[i][c] != 0), None)
        if cand is None:
            return Fraction(0)
        if cand != c:
            M[cand], M[c] = M[c], M[cand]
            det = -det
        det *= M[c][c]
        for i in range(c + 1, n):
            f = M[i][c] / M[c][c]
            for j in range(c, n):
                M[i][j] -= f * M[c][j]
    return det


def mpmath_determinant(A: list[list[Fraction]], dps: int = 80) -> mpmath.mpf:
    """High-precision determinant via mpmath (independent third party)."""
    mpmath.mp.dps = dps
    M = mpmath.matrix([[mpmath.mpf(str(v)) for v in row] for row in A])
    return mpmath.det(M)


def numpy_float64_rank(A: list[list[Fraction]]) -> int:
    Af = np.array([[float(v) for v in row] for row in A], dtype=np.float64)
    return int(np.linalg.matrix_rank(Af))


def substitute(
    A: list[list[Fraction]], x: list[Fraction]
) -> list[Fraction]:
    """A x computed with fresh loops (used by tests' own checks)."""
    m, n = len(A), len(A[0])
    out = []
    for i in range(m):
        s = Fraction(0)
        for j in range(n):
            s += A[i][j] * x[j]
        out.append(s)
    return out
