"""Fraction-free (Bareiss) elimination kernel with exact integer arithmetic.

The kernel operates on :class:`~fractions.Fraction` entries but the Bareiss
recurrence

    M'[i][j] = (M[i][j]*pivot - M[i][col]*pivot_row[j]) / prev_pivot

is an *exact-division* identity: every quotient is an integer at every step
for any leading-principal pivot sequence, including with row interchanges.
Consequently no floating point is ever introduced and coefficient growth is
explicitly bounded by a :class:`~rational_linalg.budget.DigitBudget`.

Sign contract
-------------
Row interchanges and the exact divisions are the only row operations.  Rows
are **never** multiplied by ``-1`` to "prettify" a leading sign: the sign of
every pivot, determinant minor and solution component is therefore preserved
exactly.  (``Fraction`` reduction itself only ever removes a *positive* gcd,
which cannot change sign.)

A left multiplier ``T`` is tracked alongside the matrix, so that at all times
``T @ M_original = M_current``.  The zero rows of the final echelon form give
exact left-null-space vectors directly from ``T`` (see :mod:`evidence`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable

from .budget import DigitBudget
from .errors import ResourceExhaustedError

# A log sink receives structured event dicts; the run_log module supplies one.
LogSink = Callable[[dict], None]


def _fraction_rows(rows: list[list[Fraction]]) -> list[list[str]]:
    return [[str(v) for v in row] for row in rows]


def _progress_snapshot(
    work: list[list[Fraction]],
    transform: list[list[Fraction]],
    pivot_cols: list[int],
    swaps: list[tuple[int, int]],
    budget: DigitBudget,
) -> dict:
    """Diagnosable intermediate state attached when a budget is exhausted."""
    return {
        "stage": budget.stage,
        "completed_pivots": len(pivot_cols),
        "pivot_columns": list(pivot_cols),
        "row_swaps": [list(s) for s in swaps],
        "partial_matrix": _fraction_rows(work),
        "partial_transform": _fraction_rows(transform) if transform else [],
        "budget": budget.snapshot().to_dict(),
        "digit_trace": [
            {"pivot": p, "max_digits": d} for p, d in budget.trace
        ],
    }


@dataclass
class EliminationResult:
    matrix: list[list[Fraction]]
    transform: list[list[Fraction]]
    pivot_cols: list[int]
    swaps: list[tuple[int, int]]
    rank: int
    budget: DigitBudget
    augmented_rank: int | None = None
    #: rows of T that annihilate the original matrix (indices into T), in order
    left_null_transform_rows: list[int] = field(default_factory=list)

    def echelon(self) -> list[list[Fraction]]:
        return self.matrix


def _identity(size: int) -> list[list[Fraction]]:
    return [
        [Fraction(1 if i == j else 0) for j in range(size)]
        for i in range(size)
    ]


def fraction_free_eliminate(
    matrix: list[list[Fraction]],
    *,
    budget: DigitBudget | None = None,
    track_transform: bool = True,
    sink: LogSink | None = None,
) -> EliminationResult:
    """Run Bareiss fraction-free elimination to row echelon form.

    The input matrix is copied; the caller's data is never mutated.

    Pivot strategy: among rows at or below the pivot row, the non-zero entry
    in the current column with the smallest absolute value is chosen
    (deterministic, first-row tie-break). This keeps Bareiss minors small
    without any numeric tolerance.
    """
    m = len(matrix)
    n = len(matrix[0]) if m else 0
    work = [row[:] for row in matrix]
    transform = _identity(m) if track_transform else []
    budget = budget or DigitBudget()
    swaps: list[tuple[int, int]] = []
    pivot_cols: list[int] = []

    def emit(event: str, **payload) -> None:
        if sink is not None:
            sink({"event": event, **payload})

    budget.enter_stage("input-check")
    try:
        budget.observe(value for row in work for value in row)
    except ResourceExhaustedError as exc:
        exc.progress.update(
            _progress_snapshot(work, transform, pivot_cols, swaps, budget)
        )
        raise

    prev_pivot = Fraction(1)
    pivot_col = -1

    for pivot_row in range(m):
        # Find the left-most column not already cleared below this row.
        col = pivot_col + 1
        while col < n:
            candidate = next(
                (i for i in range(pivot_row, m) if work[i][col] != 0), None
            )
            if candidate is not None:
                break
            col += 1
        if col == n:
            # Remaining rows are entirely zero.
            break

        # Smallest-abs pivot among eligible rows (no tolerances: != 0 is exact).
        choice = min(
            (i for i in range(pivot_row, m) if work[i][col] != 0),
            key=lambda i: abs(work[i][col]),
        )
        if choice != pivot_row:
            work[choice], work[pivot_row] = work[pivot_row], work[choice]
            if track_transform:
                transform[choice], transform[pivot_row] = (
                    transform[pivot_row],
                    transform[choice],
                )
            swaps.append((choice, pivot_row))
            emit(
                "row_swap",
                pivot_row=pivot_row,
                swapped_with=choice,
                column=col,
            )

        pivot_col = col
        pivot = work[pivot_row][pivot_col]
        # A negative pivot is kept as-is by contract: no sign normalisation.
        budget.enter_stage(f"eliminate-pivot-{len(pivot_cols)}")
        budget.note_pivot(len(pivot_cols))

        emit(
            "pivot_selected",
            pivot_index=len(pivot_cols),
            pivot_row=pivot_row,
            pivot_col=pivot_col,
            pivot_value=str(pivot),
            pivot_sign=("neg" if pivot < 0 else "pos"),
            prev_pivot_value=str(prev_pivot),
        )

        for i in range(pivot_row + 1, m):
            factor = work[i][pivot_col]
            # Every row below must pass through the Bareiss update, including
            # rows with factor == 0: skipping it would leave entries unscaled
            # and destroy the exact-division invariant when pivot changes.
            for j in range(pivot_col + 1, n):
                work[i][j] = (
                    work[i][j] * pivot - factor * work[pivot_row][j]
                ) / prev_pivot
            if track_transform:
                for j in range(m):
                    transform[i][j] = (
                        transform[i][j] * pivot
                        - factor * transform[pivot_row][j]
                    ) / prev_pivot
            work[i][pivot_col] = Fraction(0)

        try:
            budget.observe(value for row in work for value in row)
            if track_transform:
                budget.observe(value for row in transform for value in row)
        except ResourceExhaustedError as exc:
            exc.progress.update(
                _progress_snapshot(work, transform, pivot_cols, swaps, budget)
            )
            raise
        budget.record_pivot_trace()
        emit(
            "pivot_eliminated",
            pivot_index=len(pivot_cols),
            pivot_col=pivot_col,
            max_digits_seen=budget.max_digits_seen,
            steps_used=budget.steps_used,
        )

        pivot_cols.append(pivot_col)
        prev_pivot = pivot

    rank = len(pivot_cols)
    zero_transform_rows = list(range(rank, m)) if track_transform else []
    budget.enter_stage("done")
    emit(
        "elimination_done",
        rank=rank,
        pivot_cols=pivot_cols,
        swaps=swaps,
        max_digits_seen=budget.max_digits_seen,
    )
    return EliminationResult(
        matrix=work,
        transform=transform,
        pivot_cols=pivot_cols,
        swaps=swaps,
        rank=rank,
        budget=budget,
        left_null_transform_rows=zero_transform_rows,
    )
