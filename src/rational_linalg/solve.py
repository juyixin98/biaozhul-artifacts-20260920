"""Solution classification: unique / infinite / inconsistent systems.

The Bareiss echelon form produced by :mod:`kernel` is converted here to reduced
row echelon form (RREF) using ordinary exact rational row operations.  All
arithmetic stays in :class:`fractions.Fraction`; the digit budget continues to
be enforced and any blow-up raises with a replayable progress snapshot.

Classifications follow the Rouché--Capelli theorem exactly:

* ``rank(A) == rank(A|b) == n``  -> unique solution
* ``rank(A) == rank(A|b) <  n``  -> infinitely many solutions, returned as a
  particular point plus an exact null-space basis (one direction per free
  variable, ready to be substituted back independently)
* ``rank(A) <  rank(A|b)``       -> inconsistent; the offending echelon row's
  transform row is an exact left-null-space contradiction witness ``y`` with
  ``y^T A = 0`` and ``y^T b != 0``
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from .budget import DigitBudget
from .errors import ComputationFailedError
from .kernel import EliminationResult, fraction_free_eliminate

UNIQUE = "unique"
INFINITE = "infinite"
INCONSISTENT = "inconsistent"


def reduced_row_echelon(
    elim: EliminationResult,
    *,
    budget: DigitBudget | None = None,
    sink=None,
) -> list[list[Fraction]]:
    """Exact rational RREF of the echelon matrix.

    Rows ``0 .. rank-1`` hold the pivots (in increasing pivot columns).  Each
    pivot row is scaled by ``1/pivot`` (a whole-row scaling -- never a sign
    flip of a single entry), then entries above pivots are cleared.
    """
    budget = budget or elim.budget
    matrix = [row[:] for row in elim.matrix]
    width = len(matrix[0])
    rank = elim.rank

    budget.enter_stage("rref-normalize")
    for k in range(rank):
        col = elim.pivot_cols[k]
        pivot = matrix[k][col]
        if pivot == 0:
            raise ComputationFailedError(
                "expected non-zero pivot in echelon row",
                code="BAD_PIVOT",
                details={"row": k, "col": col},
            )
        inv = Fraction(1, 1) / pivot
        for j in range(width):
            matrix[k][j] *= inv
        budget.observe(matrix[k])

    budget.enter_stage("rref-back-eliminate")
    for k in range(rank):
        col = elim.pivot_cols[k]
        for i in range(k):
            factor = matrix[i][col]
            if factor == 0:
                continue
            for j in range(width):
                matrix[i][j] -= factor * matrix[k][j]
        budget.observe(value for row in matrix[:rank] for value in row)
        if sink is not None:
            sink(
                {
                    "event": "rref_back_step",
                    "pivot_index": k,
                    "pivot_col": col,
                    "max_digits_seen": budget.max_digits_seen,
                }
            )

    budget.enter_stage("rref-done")
    return matrix


def solve_augmented(
    A: list[list[Fraction]],
    b: list[Fraction],
    *,
    budget: DigitBudget | None = None,
    sink=None,
) -> dict[str, Any]:
    """Solve ``A x = b`` exactly.

    ``A`` is ``m x n`` (any shape, including non-square and over/underdetermined).
    Returns a structured result dict. Raises only service errors.
    """
    m = len(A)
    n = len(A[0])
    if len(b) != m:
        from .errors import InputError

        raise InputError(
            f"b has length {len(b)}, expected {m}",
            code="DIMENSION_MISMATCH",
            details={"rows": m, "b_len": len(b)},
        )

    augmented = [A[i] + [b[i]] for i in range(m)]
    elim = fraction_free_eliminate(
        augmented, budget=budget, track_transform=True, sink=sink
    )
    rhs_col = n
    coeff_pivots = [c for c in elim.pivot_cols if c < rhs_col]
    augmented_pivot_rows = [
        k for k, c in enumerate(elim.pivot_cols) if c == rhs_col
    ]
    rank_a = len(coeff_pivots)
    rank_aug = len(elim.pivot_cols)

    result: dict[str, Any] = {
        "shape": {"m": m, "n": n},
        "rank_a": rank_a,
        "rank_augmented": rank_aug,
        "pivot_columns": coeff_pivots,
        "row_swaps": [list(s) for s in elim.swaps],
        "pivot_signs": [
            "neg" if elim.matrix[k][c] < 0 else "pos"
            for k, c in enumerate(elim.pivot_cols)
        ],
        "elimination": elim,
        "budget": elim.budget,
    }

    if augmented_pivot_rows:
        # Inconsistent: echelon row is [0 ... 0 | nonzero].  Its transform row
        # y satisfies y^T A = 0 and y^T b != 0.
        witness_row = augmented_pivot_rows[0]
        y = elim.transform[witness_row]
        rhs_value = elim.matrix[witness_row][rhs_col]
        result.update(
            classification=INCONSISTENT,
            contradiction_witness=y,
            contradiction_rhs=Fraction(rhs_value),
            witness_echelon_row=witness_row,
        )
        return result

    rref = reduced_row_echelon(elim, budget=budget, sink=sink)
    free_cols = [c for c in range(n) if c not in set(coeff_pivots)]

    particular = [Fraction(0) for _ in range(n)]
    for k, c in enumerate(coeff_pivots):
        particular[c] = rref[k][rhs_col]

    null_basis: list[list[Fraction]] = []
    for f in free_cols:
        vector = [Fraction(0) for _ in range(n)]
        vector[f] = Fraction(1)
        for k, c in enumerate(coeff_pivots):
            vector[c] = -rref[k][f]
        null_basis.append(vector)

    if not free_cols:
        result.update(classification=UNIQUE, particular=particular, null_basis=[])
    else:
        result.update(
            classification=INFINITE,
            particular=particular,
            null_basis=null_basis,
            free_columns=free_cols,
        )
    result["rref"] = rref
    return result


def rank_of(
    A: list[list[Fraction]],
    *,
    budget: DigitBudget | None = None,
    sink=None,
) -> dict[str, Any]:
    """Exact rank and left null space of ``A`` (m x n, any shape)."""
    m = len(A)
    n = len(A[0])
    elim = fraction_free_eliminate(
        A, budget=budget, track_transform=True, sink=sink
    )
    left_nullspace = [elim.transform[k] for k in range(elim.rank, m)]
    return {
        "shape": {"m": m, "n": n},
        "rank": elim.rank,
        "nullity": n - elim.rank,
        "left_nullity": m - elim.rank,
        "pivot_columns": elim.pivot_cols,
        "row_swaps": [list(s) for s in elim.swaps],
        "pivot_signs": [
            "neg" if elim.matrix[k][c] < 0 else "pos"
            for k, c in enumerate(elim.pivot_cols)
        ],
        "left_nullspace": left_nullspace,
        "elimination": elim,
        "budget": elim.budget,
    }
