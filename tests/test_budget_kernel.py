"""Unit tests for the digit budget and the fraction-free kernel."""

from __future__ import annotations

import random
from fractions import Fraction

import pytest

from rational_linalg.budget import DigitBudget, decimal_digits
from rational_linalg.errors import ResourceExhaustedError
from rational_linalg.kernel import fraction_free_eliminate

from .independent_oracle import gauss_jordan_rref, oracle_det


def test_decimal_digits_counts_magnitude():
    assert decimal_digits(0) == 1
    assert decimal_digits(9) == 1
    assert decimal_digits(10) == 2
    assert decimal_digits(-10**99) == 100


def test_budget_input_oversize_fails_with_progress():
    budget = DigitBudget(digit_limit=3)
    A = [[Fraction(10000)]]  # 5 digits > 3
    with pytest.raises(ResourceExhaustedError) as exc:
        fraction_free_eliminate(A, budget=budget)
    assert exc.value.code == "DIGIT_BUDGET_EXCEEDED"
    progress = exc.value.progress
    assert progress["stage"] == "input-check"
    assert progress["budget"]["digit_limit"] == 3
    assert progress["partial_matrix"] == [["10000"]]


def test_budget_growth_mid_elimination_fails_with_snapshot():
    # Hilbert-like matrix forces big minors; a tiny budget must trip mid-run,
    # and the snapshot must show how many pivots completed.
    n = 5
    H = [[Fraction(1, i + j + 1) for j in range(n)] for i in range(n)]
    budget = DigitBudget(digit_limit=2)
    with pytest.raises(ResourceExhaustedError) as exc:
        fraction_free_eliminate(H, budget=budget)
    progress = exc.value.progress
    assert 0 <= progress["completed_pivots"] < n
    assert progress["partial_matrix"]  # non-empty intermediate matrix
    # The trip may happen inside the first pivot update (before any pivot is
    # recorded) or later; in every case the ledger saw growth past the limit.
    assert progress["budget"]["max_digits_seen"] > 2
    if progress["digit_trace"]:
        assert progress["digit_trace"][-1]["max_digits"] > 2


def test_step_budget_distinct_code():
    A = [[Fraction(1), Fraction(2)], [Fraction(3), Fraction(4)]]
    budget = DigitBudget(digit_limit=4096, step_limit=0)
    with pytest.raises(ResourceExhaustedError) as exc:
        fraction_free_eliminate(A, budget=budget)
    assert exc.value.code == "STEP_BUDGET_EXCEEDED"


def test_kernel_matches_independent_rref_rank_on_random_matrices():
    rng = random.Random(20260927)
    for m, n in [(1, 1), (2, 3), (3, 2), (4, 4), (5, 3), (3, 5)]:
        for _ in range(30):
            A = [
                [Fraction(rng.randint(-9, 9)) for _ in range(n)]
                for _ in range(m)
            ]
            result = fraction_free_eliminate(
                [row[:] for row in A], track_transform=True
            )
            _, oracle_pivots, _ = gauss_jordan_rref(A)
            assert result.rank == len(oracle_pivots), (m, n, A)
            # T @ A must equal the current work matrix exactly.
            for i in range(m):
                for j in range(n):
                    got = sum(
                        (result.transform[i][k] * A[k][j] for k in range(m)),
                        Fraction(0),
                    )
                    assert got == result.matrix[i][j]
            # Fraction-free contract: every T and work entry stays an integer
            # (the Bareiss quotient is an exact-division identity, never a
            # fraction introduced by the algorithm).
            for row in result.matrix:
                for v in row:
                    assert v.denominator == 1, (v, A)
            for row in result.transform:
                for v in row:
                    assert v.denominator == 1, (v, A)


def test_kernel_determinant_sign_contract_antidiagonal():
    # Requires a row swap; det = -1 and the negative sign must survive.
    A = [[Fraction(0), Fraction(1)], [Fraction(1), Fraction(0)]]
    result = fraction_free_eliminate(A)
    assert result.swaps == [(1, 0)]
    final = result.matrix[1][result.pivot_cols[-1]]
    signed_det = final * (-1 if len(result.swaps) % 2 else 1)
    assert signed_det == -1
    assert oracle_det(A) == -1


def test_negative_pivot_is_never_sign_flipped():
    A = [[Fraction(-2), Fraction(1)], [Fraction(4), Fraction(-1)]]
    events = []
    result = fraction_free_eliminate(A, sink=events.append)
    first_pivot = result.matrix[0][result.pivot_cols[0]]
    assert first_pivot == -2
    pivot_events = [e for e in events if e["event"] == "pivot_selected"]
    assert pivot_events[0]["pivot_sign"] == "neg"
    # Reduction never scales a row by -1: det stays -2.
    assert oracle_det(A) == -2


def test_zero_rows_yield_exact_left_nullspace_transform_rows():
    # row3 = row1 + row2  => after elimination one transform row annihilates A.
    A = [
        [Fraction(1), Fraction(2), Fraction(3)],
        [Fraction(4), Fraction(5), Fraction(6)],
        [Fraction(5), Fraction(7), Fraction(9)],
    ]
    result = fraction_free_eliminate(A, track_transform=True)
    assert result.rank == 2
    idx = result.left_null_transform_rows[0]
    y = result.transform[idx]
    for j in range(3):
        assert sum(y[i] * A[i][j] for i in range(3)) == 0
