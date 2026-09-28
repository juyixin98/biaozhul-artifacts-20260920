"""Fraction-free elimination kernel.

Two-phase exact elimination over an integer augmented matrix ``[A | b*]``:

**Phase 1 - fraction-free Bareiss forward elimination.**
Every coefficient stays an integer (no ``Fraction``, no float).  The update

    a'_ij = (pivot * a_ij - factor * pivot_row_j) / previous_pivot

uses an *exact* division: the remainder is asserted zero, so a violated
Bareiss invariant becomes ``COMPUTATION_FAILED`` instead of a silently wrong
answer.  Pivots are chosen by smallest absolute non-zero entry below the
frontier (coefficient-growth control).  A row swap is recorded - the code
never multiplies a row by -1, so signs are preserved and the determinant only
acquires a factor of ``(-1) ** len(swaps)``.  Before elimination starts, each
row is divided by the gcd of *all* its entries (positive content divisor
only) - the direct remedy for matrices carrying a large common factor.

**Phase 2 - rational RREF normalization.**
Starting from the integer upper-triangular result, pivot rows are normalized
to pivot value 1 and entries above each pivot are cancelled using exact
:class:`~fractions.Fraction` arithmetic.  Coefficient growth is bounded here
because the heavy lifting already happened over integers.

A separate exact witness matrix ``W`` mirrors *every* row operation as
fractions and is initialized against the caller's *true* (pre-denominator-
clearing) rows, giving the independently checkable identity

    RREF([A | b]) == W @ true_input_rows

and therefore left-null-space witnesses ``y`` with ``y^T A == 0`` and
``y^T b != 0`` for inconsistent systems.

A budget callback inspects a :class:`ProgressSnapshot` after every step of
either phase and may raise ``BudgetExhausted`` with replayable intermediate
state - the kernel never falls back to floating point.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from math import gcd, lcm
from typing import Callable, Sequence

from .errors import ComputationFailed

#: Signature of a budget hook: inspect the snapshot, raise BudgetExhausted.
BudgetHook = Callable[["ProgressSnapshot"], None]


def decimal_digits_bound(x: int) -> int:
    """Cheap safe upper bound on the decimal digit count of ``abs(x)``."""
    if x == 0:
        return 1
    return (x.bit_length() * 301) // 1000 + 2


@dataclass
class ProgressSnapshot:
    """Serializable intermediate state for budget hooks and replay logs."""

    phase: str  # "forward" | "normalize"
    step: int
    column: int
    pivot_row: int
    rank_so_far: int
    previous_pivot: int
    pivots: list[tuple[int, int]]
    swaps: list[tuple[int, int]]
    max_decimal_digits: int
    matrix: list[list[str]]  # exact decimal (forward) or fraction strings

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "step": self.step,
            "column": self.column,
            "pivot_row": self.pivot_row,
            "rank_so_far": self.rank_so_far,
            "previous_pivot": str(self.previous_pivot),
            "pivots": [[r, c] for r, c in self.pivots],
            "swaps": [[i, j] for i, j in self.swaps],
            "max_decimal_digits": self.max_decimal_digits,
            "matrix": self.matrix,
        }


@dataclass
class EliminationResult:
    rows: int
    cols: int  # coefficient columns only
    rank: int
    rref: list[list[Fraction]]           # full augmented width
    pivots: list[tuple[int, int]]
    pivot_columns: list[int]
    swaps: list[tuple[int, int]]
    witness: list[list[Fraction]]        # P, shape m x m
    content_divisors: list[int]          # per-row gcd removed before Bareiss
    determinant_reduced: int | None      # det of content-reduced integer matrix
    determinant_original: Fraction | None  # det of the true (rational) input A
    step_records: list[dict] = field(default_factory=list)


def _row_gcd(row: Sequence[int]) -> int:
    g = 0
    for v in row:
        g = gcd(g, abs(v))
        if g == 1:
            return 1
    return g


def _exact_div(num: int, den: int) -> int:
    q, r = divmod(num, den)
    if r != 0:
        raise ComputationFailed(
            "Bareiss exact-division invariant violated",
            {"numerator": str(num), "denominator": str(den), "remainder": str(r)},
        )
    return q


def _int_snapshot(
    phase: str,
    step: int,
    column: int,
    pivot_row: int,
    previous_pivot: int,
    pivots: list[tuple[int, int]],
    swaps: list[tuple[int, int]],
    mmat: list[list[int]],
) -> ProgressSnapshot:
    worst = 1
    for row in mmat:
        for v in row:
            d = decimal_digits_bound(v)
            if d > worst:
                worst = d
    return ProgressSnapshot(
        phase, step, column, pivot_row, len(pivots), previous_pivot,
        list(pivots), list(swaps), worst,
        [[str(v) for v in row] for row in mmat],
    )


def eliminate(
    matrix: Sequence[Sequence[int]],
    coefficient_cols: int,
    budget_hook: BudgetHook | None = None,
    input_row_multipliers: Sequence[int] | None = None,
) -> EliminationResult:
    """Eliminate an integer augmented matrix to exact RREF.

    Columns ``0 .. coefficient_cols-1`` are the coefficient block ``A``;
    remaining columns are an arbitrary augmented block (typically one rhs).
    Pivot search covers coefficient columns only.

    ``input_row_multipliers`` optionally records that ``matrix[i]`` was
    obtained by multiplying the caller's *true* (possibly rational) row ``i``
    by a positive integer ``mult[i]`` (e.g. denominator clearing).  When given,
    the witness matrix is initialized against the true rows, so the invariant
    is ``RREF == P @ true_input`` directly; when omitted it defaults to all
    ones (``RREF == P @ matrix``).
    """
    if not matrix:
        raise ComputationFailed("eliminate() called with no rows")
    m = len(matrix)
    total_cols = len(matrix[0])
    n = coefficient_cols
    if n <= 0 or total_cols < n:
        raise ComputationFailed(
            "bad coefficient_cols / matrix width",
            {"coefficient_cols": n, "width": total_cols},
        )
    if any(len(r) != total_cols for r in matrix):
        raise ComputationFailed("ragged matrix passed to eliminate()")

    mmat: list[list[int]] = [list(row) for row in matrix]
    if input_row_multipliers is None:
        row_mult = [1] * m
    else:
        row_mult = list(input_row_multipliers)
        if len(row_mult) != m or any(x <= 0 for x in row_mult):
            raise ComputationFailed(
                "input_row_multipliers must be one positive integer per row",
                {"got": [str(x) for x in row_mult]},
            )
    # Witness W invariant (maintained through every step below):
    #   every current matrix row == W_row (a linear combination) of the
    #   caller's TRUE input rows.  Initially M_int[i] = mult[i] * true_row[i],
    #   so W starts as diag(mult_i); the per-row content division by g_i then
    #   divides both the data row and W row i by g_i.
    pwit: list[list[Fraction]] = [
        [Fraction(row_mult[i] if i == j else 0) for j in range(m)]
        for i in range(m)
    ]

    # -- Per-row content reduction (positive divisor: sign cannot change). --
    content_divisors: list[int] = []
    for i in range(m):
        g = _row_gcd(mmat[i])
        content_divisors.append(g if g else 1)
        if g > 1:
            mmat[i] = [v // g for v in mmat[i]]
            pwit[i] = [v / g for v in pwit[i]]

    pivots: list[tuple[int, int]] = []
    swaps: list[tuple[int, int]] = []
    step_records: list[dict] = []

    def record(snap: ProgressSnapshot) -> None:
        step_records.append(snap.to_dict())
        if budget_hook is not None:
            budget_hook(snap)

    record(_int_snapshot("forward", 0, -1, 0, 1, pivots, swaps, mmat))

    # ----- Phase 1: classic fraction-free Bareiss forward elimination. -----
    previous_pivot = 1
    frontier = 0
    for col in range(n):
        if frontier >= m:
            break
        choice = -1
        best: int | None = None
        for i in range(frontier, m):
            v = mmat[i][col]
            if v != 0 and (best is None or abs(v) < best):
                best, choice = abs(v), i
        if choice == -1:
            continue  # zero column below frontier -> free variable

        if choice != frontier:
            mmat[choice], mmat[frontier] = mmat[frontier], mmat[choice]
            pwit[choice], pwit[frontier] = pwit[frontier], pwit[choice]
            swaps.append((choice, frontier))

        pivot = mmat[frontier][col]
        for i in range(frontier + 1, m):
            factor = mmat[i][col]
            # NOTE: there is deliberately no ``factor == 0`` shortcut here.
            # Even when factor vanishes the Bareiss/Sylvester update still
            # multiplies every remaining entry by pivot/previous_pivot;
            # skipping it breaks the exact-division invariant of later steps.
            prow = mmat[frontier]
            row = mmat[i]
            for j in range(col + 1, total_cols):
                row[j] = _exact_div(
                    pivot * row[j] - factor * prow[j], previous_pivot
                )
            # Column col cancels identically; columns < col are already zero.
            row[col] = 0
            for j in range(col):
                row[j] = 0
            pi, pk = pwit[i], pwit[frontier]
            pwit[i] = [
                (pivot * pi[j] - factor * pk[j]) / previous_pivot
                for j in range(m)
            ]

        previous_pivot = pivot
        pivots.append((frontier, col))
        frontier += 1
        record(_int_snapshot(
            "forward", len(pivots), col, frontier, pivot, pivots, swaps, mmat
        ))

    rank = len(pivots)

    # Non-pivot rows (indices rank..m-1) now have an all-zero coefficient part.
    # Bareiss scaling can have multiplied them by later pivots (a zero-factor
    # row is still rescaled by pivot/previous_pivot), so reduce each such row
    # by the gcd of ALL its entries (positive divisor: sign preserved) and
    # mirror it on the witness.  This yields primitive contradiction rows
    # [0..0 | c] (or exact zero rows) with small, canonical evidence.
    for i in range(rank, m):
        g = _row_gcd(mmat[i])
        if g > 1:
            mmat[i] = [v // g for v in mmat[i]]
            pwit[i] = [v / g for v in pwit[i]]

    # ----- Phase 2: exact rational normalization to RREF. -----
    rref: list[list[Fraction]] = [[Fraction(v) for v in row] for row in mmat]

    def frac_snapshot(step: int, col: int) -> ProgressSnapshot:
        worst = 1
        strings: list[list[str]] = []
        for row in rref:
            srow = []
            for v in row:
                srow.append(str(v))
                d = max(decimal_digits_bound(v.numerator),
                        decimal_digits_bound(v.denominator))
                if d > worst:
                    worst = d
            strings.append(srow)
        return ProgressSnapshot(
            "normalize", step, col, rank, rank, 1, pivots, swaps, worst, strings
        )

    # Normalize pivot rows bottom-up, cancelling entries above each pivot.
    for k in range(rank - 1, -1, -1):
        pr, pc = pivots[k]
        pv = rref[pr][pc]
        if pv == 0:
            raise ComputationFailed(
                "pivot vanished in normalization",
                {"pivot_index": k, "row": pr, "column": pc},
            )
        if pv != 1:
            rref[pr] = [v / pv for v in rref[pr]]
            pwit[pr] = [v / pv for v in pwit[pr]]
        pivot_row = rref[pr]
        for i in range(pr):
            f = rref[i][pc]
            if f == 0:
                continue
            rref[i] = [rref[i][j] - f * pivot_row[j] for j in range(total_cols)]
            pw = pwit[i]
            pwp = pwit[pr]
            pwit[i] = [pw[j] - f * pwp[j] for j in range(m)]
        record(frac_snapshot(rank - k, pc))

    # Presentation normalization of zero-coefficient (non-pivot) rows: scale
    # each such row by a NON-ZERO factor so its left-combination witness
    # becomes an integer primitive vector whose first non-zero entry is
    # positive.  The RREF row is scaled by the same factor, preserving the
    # RREF == W @ true_input invariant; this only fixes the *representation*
    # of a 0=0 / 0=c row (canonical small contradiction evidence) and never
    # changes signs silently - the factor is recorded in the step log.
    for i in range(rank, m):
        denoms = [v.denominator for v in pwit[i]]
        common = lcm(*denoms) if denoms else 1
        yfrac = [x * common for x in pwit[i]]
        if any(v.denominator != 1 for v in yfrac):
            raise ComputationFailed(  # defensive: lcm must clear denominators
                "witness denominator clearing failed", {"row": i}
            )
        yint = [int(v) for v in yfrac]
        g = 0
        for v in yint:
            g = gcd(g, abs(v))
        scale = Fraction(common, g) if g > 0 else Fraction(common)
        first_nz = next((v for v in yint if v != 0), None)
        if first_nz is not None and first_nz < 0:
            scale = -scale
        if scale != 1:
            rref[i] = [v * scale for v in rref[i]]
            pwit[i] = [v * scale for v in pwit[i]]
            record(frac_snapshot(rank + i + 1, -1))

    # Determinants (square, full rank).  Bareiss's final pivot is the
    # determinant of the content-reduced matrix up to swap signs.  The reduced
    # matrix is diag(mult_i / g_i) @ true_A, hence
    #   det(true_A) = det(reduced) * prod(g_i) / prod(mult_i).
    determinant_reduced: int | None = None
    determinant_original: Fraction | None = None
    if m == n and rank == n:
        last_pr, last_pc = pivots[-1]
        raw_final_pivot = mmat[last_pr][last_pc]
        sign = -1 if len(swaps) % 2 else 1
        determinant_reduced = sign * raw_final_pivot
        gprod = 1
        for g in content_divisors:
            gprod *= g
        mprod = 1
        for mv in row_mult:
            mprod *= mv
        determinant_original = Fraction(
            determinant_reduced * gprod, mprod
        )

    return EliminationResult(
        rows=m,
        cols=n,
        rank=rank,
        rref=rref,
        pivots=pivots,
        pivot_columns=[c for _, c in pivots],
        swaps=swaps,
        witness=pwit,
        content_divisors=content_divisors,
        determinant_reduced=determinant_reduced,
        determinant_original=determinant_original,
        step_records=step_records,
    )
