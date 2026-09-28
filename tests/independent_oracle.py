"""Independent reference oracle.

This is deliberately a *separate, naive* implementation from the production
kernel: ordinary Gauss-Jordan elimination over ``fractions.Fraction`` using
whole-row normalisation (no Bareiss, no shared helpers beyond parsing).
Expected answers in tests must not be produced by the code under test, so
randomised cross-checks derive their truth from this module.
"""

from __future__ import annotations

import random
from fractions import Fraction
from typing import List, Tuple

Matrix = List[List[Fraction]]
Vector = List[Fraction]


def gauss_jordan_rref(mat: Matrix) -> Tuple[Matrix, List[int], List[Tuple[int, int]]]:
    """Return (RREF copy, pivot columns, swaps). Naive independent algorithm."""
    m, n = len(mat), len(mat[0])
    work = [row[:] for row in mat]
    pivots: List[int] = []
    swaps: List[Tuple[int, int]] = []
    r = 0
    for c in range(n):
        candidate = next((i for i in range(r, m) if work[i][c] != 0), None)
        if candidate is None:
            continue
        if candidate != r:
            work[r], work[candidate] = work[candidate], work[r]
            swaps.append((candidate, r))
        scale = work[r][c]
        work[r] = [v / scale for v in work[r]]
        for i in range(m):
            if i == r:
                continue
            factor = work[i][c]
            if factor != 0:
                work[i] = [a - factor * b for a, b in zip(work[i], work[r])]
        pivots.append(c)
        r += 1
        if r == m:
            break
    return work, pivots, swaps


def oracle_rank(A: Matrix) -> int:
    _, pivots, _ = gauss_jordan_rref(A)
    return len(pivots)


def oracle_solve(
    A: Matrix, b: Vector
) -> Tuple[str, object]:
    """Classify and solve. Returns (classification, payload).

    payload shapes:
      unique       -> Fraction vector
      infinite     -> (particular vector, null-space basis)
      inconsistent -> witness vector y (first zero-row's identity tracking row)
    Identity tracking here uses the same row operations applied to I, but via
    this module's own code path (independent of production kernel).
    """
    m, n = len(A), len(A[0])
    aug = [A[i] + [b[i]] for i in range(m)]
    # Track left operations with an independent augmented identity block.
    tracked = [aug[i] + [Fraction(1 if i == k else 0) for k in range(m)]
              for i in range(m)]
    rref, pivots, _ = gauss_jordan_rref(tracked)
    coeff_pivots = [c for c in pivots if c < n]
    rank_a = len(coeff_pivots)
    inconsistent_row = next(
        (i for i, c in enumerate(pivots) if c == n), None
    )
    if inconsistent_row is not None:
        y = rref[inconsistent_row][n + 1 :]
        return "inconsistent", y

    free = [c for c in range(n) if c not in set(coeff_pivots)]
    particular = [Fraction(0) for _ in range(n)]
    for k, c in enumerate(coeff_pivots):
        particular[c] = rref[k][n]
    basis = []
    for f in free:
        v = [Fraction(0) for _ in range(n)]
        v[f] = Fraction(1)
        for k, c in enumerate(coeff_pivots):
            v[c] = -rref[k][f]
        basis.append(v)
    if free:
        return "infinite", (particular, basis)
    return "unique", particular


def oracle_det(A: Matrix) -> Fraction:
    """Determinant via fraction-keeping Gaussian elimination (independent)."""
    n = len(A)
    work = [row[:] for row in A]
    sign = 1
    for c in range(n):
        candidate = next((i for i in range(c, n) if work[i][c] != 0), None)
        if candidate is None:
            return Fraction(0)
        if candidate != c:
            work[c], work[candidate] = work[candidate], work[c]
            sign *= -1
        pivot = work[c][c]
        for i in range(c + 1, n):
            factor = work[i][c] / pivot
            for j in range(c + 1, n):
                work[i][j] -= factor * work[c][j]
            work[i][c] = Fraction(0)
    value = Fraction(sign)
    for c in range(n):
        value *= work[c][c]
    return value


def random_integer_matrix(
    rng: random.Random, m: int, n: int, bound: int = 9
) -> Matrix:
    return [
        [Fraction(rng.randint(-bound, bound)) for _ in range(n)]
        for _ in range(m)
    ]


def random_consistent_system(
    rng: random.Random, m: int, n: int, bound: int = 9
) -> Tuple[Matrix, Vector, Vector]:
    """Build A x = b from a hidden x, guaranteeing consistency."""
    A = random_integer_matrix(rng, m, n, bound)
    x_true = [Fraction(rng.randint(-5, 5)) for _ in range(n)]
    b = [
        sum((a * xi for a, xi in zip(row, x_true)), Fraction(0))
        for row in A
    ]
    return A, b, x_true
