"""Independent exact evidence: substitution checks, contradiction witnesses.

Every certificate returned by the solver can be re-checked here using *only*
the original inputs -- these helpers never trust the solver's own bookkeeping.
All arithmetic is exact (:class:`fractions.Fraction`).  mpmath is used solely
to render human-readable high-precision decimal strings, never to make a
correctness decision.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Sequence

import mpmath  # display-only high precision

from .kernel import EliminationResult

Vector = list[Fraction]
Matrix = list[list[Fraction]]


def matvec(A: Matrix, x: Vector) -> Vector:
    return [sum((a * xi for a, xi in zip(row, x)), Fraction(0)) for row in A]


def dot(u: Sequence[Fraction], v: Sequence[Fraction]) -> Fraction:
    return sum((a * b for a, b in zip(u, v)), Fraction(0))


def vec_sub(u: Vector, v: Vector) -> Vector:
    return [a - b for a, b in zip(u, v)]


def is_zero_vector(v: Sequence[Fraction]) -> bool:
    return all(a == 0 for a in v)


def verify_solution(
    A: Matrix,
    b: Vector,
    particular: Vector,
    null_basis: list[Vector],
    parameters: Sequence[Fraction] | None = None,
) -> dict[str, Any]:
    """Exactly verify ``A(particular + sum t_j v_j) == b``.

    With no parameters the particular point itself is substituted.  The check
    is two-sided evidence:

    * ``A @ particular == b``
    * ``A @ v_j == 0`` for every null-space direction
    """
    checks: dict[str, Any] = {}
    residual_p = vec_sub(matvec(A, particular), b)
    checks["particular_residual"] = residual_p
    checks["particular_ok"] = is_zero_vector(residual_p)

    null_residuals = [matvec(A, v) for v in null_basis]
    checks["null_residuals"] = null_residuals
    checks["null_ok"] = all(is_zero_vector(r) for r in null_residuals)

    if parameters is not None:
        if len(parameters) != len(null_basis):
            raise ValueError("one parameter per null-space vector is required")
        x = particular[:]
        for t, v in zip(parameters, null_basis):
            for i in range(len(x)):
                x[i] += t * v[i]
        residual = vec_sub(matvec(A, x), b)
        checks["parameterized_point"] = x
        checks["parameterized_residual"] = residual
        checks["parameterized_ok"] = is_zero_vector(residual)

    checks["ok"] = checks["particular_ok"] and checks["null_ok"] and checks.get(
        "parameterized_ok", True
    )
    return checks


def verify_contradiction_witness(
    A: Matrix, b: Vector, y: Vector
) -> dict[str, Any]:
    """Exactly verify a left-null-space certificate of inconsistency.

    Requires ``y^T A == 0`` (componentwise) and ``y^T b != 0``.  Such a ``y``
    proves independently that no ``x`` can satisfy ``A x = b``.
    """
    if len(y) != len(A):
        raise ValueError("witness length must equal number of rows of A")
    yt_a: Vector = [
        sum((y[i] * A[i][j] for i in range(len(A))), Fraction(0))
        for j in range(len(A[0]))
    ]
    yt_b = dot(y, b)
    return {
        "y": y,
        "yt_A": yt_a,
        "yt_b": yt_b,
        "annihilates_a": is_zero_vector(yt_a),
        "contradicts_b": yt_b != 0,
        "ok": is_zero_vector(yt_a) and yt_b != 0,
    }


def verify_left_nullspace(
    A: Matrix, vectors: list[Vector]
) -> dict[str, Any]:
    """Exactly verify ``y^T A == 0`` for rank-service left null vectors."""
    residuals = [
        [
            sum((y[i] * A[i][j] for i in range(len(A))), Fraction(0))
            for j in range(len(A[0]))
        ]
        for y in vectors
    ]
    return {
        "residuals": residuals,
        "ok": all(is_zero_vector(r) for r in residuals),
    }


def fraction_free_determinant(
    elim: EliminationResult, size: int
) -> Fraction | None:
    """Exact determinant of an ``n x n`` matrix from completed Bareiss.

    With row interchanges the Bareiss final pivot equals
    ``(-1)^(swap count) det(A)``; hence ``det(A) = (-1)^swaps * final pivot``.
    Returns ``None`` when the matrix is rank deficient.
    """
    if elim.rank != size:
        return None
    last = elim.pivot_cols[-1]
    final_pivot = elim.matrix[size - 1][last]
    return final_pivot * (-1 if len(elim.swaps) % 2 else 1)


def to_decimal(value: Fraction | int, dps: int = 40) -> str:
    """Render an exact rational as a fixed high-precision decimal string.

    Display only: mpmath performs the division at ``dps`` digits; this string
    must never be fed back into a correctness decision.
    """
    with mpmath.workdps(dps):
        q = mpmath.mpf(value.numerator) / mpmath.mpf(value.denominator)
        text = mpmath.nstr(q, dps, strip_zeros=False)
    return text


def fraction_to_payload(value: Fraction, dps: int = 40) -> dict[str, Any]:
    return {
        "fraction": f"{value.numerator}/{value.denominator}",
        "num": value.numerator,
        "den": value.denominator,
        "decimal": to_decimal(value, dps=dps),
    }
