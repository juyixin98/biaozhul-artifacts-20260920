"""Kernel tests: fraction-free elimination, invariants, sign/swaps, budget.

Expectations come from tests.oracle (independent Fraction/mpmath/numpy code),
never from the production kernel itself.
"""
from __future__ import annotations

import random
from fractions import Fraction

import pytest

from rationalsvc import evidence, kernel
from rationalsvc.errors import BudgetExhausted, ComputationFailed
from rationalsvc.frac import common_denominator

from . import oracle


def to_integer_system(A, b):
    """Denominator clearing exactly like the runner; return rows, multipliers."""
    m = len(A)
    rows: list[list[int]] = []
    mults: list[int] = []
    for i in range(m):
        vals = list(A[i]) + list(b[i])
        d = common_denominator(vals)
        rows.append([int(v * d) for v in vals])
        mults.append(d)
    return rows, mults


def rand_system(rng, max_dim=6, bound=6):
    m = rng.randint(1, max_dim)
    n = rng.randint(1, max_dim)
    k = rng.randint(1, 2)
    A = [
        [Fraction(rng.randint(-bound, bound), rng.randint(1, 4))
         for _ in range(n)]
        for _ in range(m)
    ]
    b = [
        [Fraction(rng.randint(-bound, bound), rng.randint(1, 4))
         for _ in range(k)]
        for _ in range(m)
    ]
    return A, b


# ---------------------------------------------------------------------------
# Deterministic concrete cases
# ---------------------------------------------------------------------------

def test_unique_two_by_two_concrete_values():
    # 2x + y = 1 ; x + 3y = 2  ->  x = 1/5, y = 3/5 ; det = 5
    res = kernel.eliminate([[2, 1, 1], [1, 3, 2]], 2)
    assert res.rank == 2
    assert res.determinant_original == 5
    assert res.rref[0] == [Fraction(1), Fraction(0), Fraction(1, 5)]
    assert res.rref[1] == [Fraction(0), Fraction(1), Fraction(3, 5)]


def test_row_swap_flips_only_sign_not_magnitude():
    # [[0,1],[2,3]] forces one swap; determinant must be -2 (sign flip only).
    res = kernel.eliminate([[0, 1, 7], [2, 3, 11]], 2)
    assert res.swaps == [(1, 0)]
    assert res.determinant_original == -2
    # solution x=-5, y=7
    assert res.rref[0][2] == -5
    assert res.rref[1][2] == 7


def test_row_content_reduction_removes_common_factor_without_sign_change():
    # rows carry common factors 6 and 10; result still x=2, y=1.
    res = kernel.eliminate([[12, 18, 42], [20, -10, 30]], 2)
    assert res.content_divisors == [6, 10]
    assert res.rref[0][2] == 2
    assert res.rref[1][2] == 1
    assert res.determinant_original == -480  # determinant of the given matrix


def test_inconsistent_system_witness_is_primitive_and_sign_conventional():
    # x+y+z = 1 ; x+y+z = 3 ; 2x-y+z = 0
    A = [[1, 1, 1], [1, 1, 1], [2, -1, 1]]
    b = [[1], [3], [0]]
    rows, mults = to_integer_system(A, b)
    res = kernel.eliminate(rows, 3, input_row_multipliers=mults)
    sols = evidence.extract_solutions(res, b)
    assert sols[0].classification == evidence.INCONSISTENT
    y = sols[0].left_null_vector
    assert y == [Fraction(1), Fraction(-1), Fraction(0)]  # primitive, + first
    # y_dot_b is populated by the independent verification pass
    evidence.independently_verify(A, b, sols)
    assert sols[0].y_dot_b == -2


def test_witness_independently_substituted_is_zero_left_nonzero_right():
    A = [[1, 1, 1], [1, 1, 1], [2, -1, 1]]
    b = [[1], [3], [0]]
    rows, mults = to_integer_system(A, b)
    res = kernel.eliminate(rows, 3, input_row_multipliers=mults)
    sols = evidence.extract_solutions(res, b)
    report = evidence.independently_verify(A, b, sols)
    assert report.all_residuals_zero is True
    assert report.witness_y_dot_A[0] == ["0", "0", "0"]
    assert report.witness_y_dot_b[0] == "-2"


def test_parametric_infinite_solution_concrete():
    # x+y+z=1 ; 2x+2y+2z=2 ; x+z=1 -> y=0 free? actually free column z,
    # particular (1,0,0), null vector (-1,0,1)
    A = [[1, 1, 1], [2, 2, 2], [1, 0, 1]]
    b = [[1], [2], [1]]
    rows, mults = to_integer_system(A, b)
    res = kernel.eliminate(rows, 3, input_row_multipliers=mults)
    sols = evidence.extract_solutions(res, b)
    assert sols[0].classification == evidence.INFINITE
    assert sols[0].particular == [Fraction(1), 0, 0]
    assert sols[0].nullspace_basis == [[Fraction(-1), 0, 1]]


def test_negative_entries_keep_their_sign_through_normalization():
    # [[1,2],[3,4]] det -2; solve against a chosen b
    res = kernel.eliminate([[1, 2, 5], [3, 4, 6]], 2)
    assert res.determinant_original == -2
    # x = (2*6 - 4*5)/-2? solve directly: x+2y=5, 3x+4y=6 -> x=-4, y=9/2
    assert res.rref[0][2] == -4
    assert res.rref[1][2] == Fraction(9, 2)


# ---------------------------------------------------------------------------
# Differential fuzz against the independent oracle
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(40))
def test_differential_against_independent_fraction_oracle(seed):
    rng = random.Random(9000 + seed)
    A, b = rand_system(rng)
    m, n, k = len(A), len(A[0]), len(b[0])
    rows, mults = to_integer_system(A, b)

    res = kernel.eliminate(rows, n, input_row_multipliers=mults)
    Mexp, pexp = oracle.rref_fraction(A, b)

    # rank agrees
    assert res.rank == len(pexp)

    # the witness invariant RREF == W @ true rational input
    for i in range(m):
        for j in range(n + k):
            true_col = [A[t][j] if j < n else b[t][j - n] for t in range(m)]
            got = sum(res.witness[i][t] * true_col[t] for t in range(m))
            assert got == res.rref[i][j]

    sols = evidence.extract_solutions(res, b)
    report = evidence.independently_verify(A, b, sols)
    assert report.all_residuals_zero

    # per-rhs classification agrees with the oracle's own classification
    for r in range(k):
        bcol = [b[i][r] for i in range(m)]
        exp_cls, exp_x0, exp_y = oracle.classify_rhs(A, bcol)
        assert sols[r].classification == exp_cls
        if exp_cls != "inconsistent":
            assert sols[r].particular is not None
            # particular vector substitutes to exactly b
            assert oracle.substitute(A, sols[r].particular) == bcol
            # every null-space vector substitutes to exactly zero
            for v in sols[r].nullspace_basis:
                assert all(x == 0 for x in oracle.substitute(A, v))
        else:
            # independently constructed oracle witness also proves it
            yta = [sum(exp_y[i] * A[i][j] for i in range(m)) for j in range(n)]
            assert all(x == 0 for x in yta)
            assert sum(exp_y[i] * bcol[i] for i in range(m)) != 0

    # when every rhs consistent, the full augmented RREF matches the oracle
    if all(sols[r].classification != evidence.INCONSISTENT for r in range(k)):
        got = sorted(tuple(map(str, row)) for row in res.rref)
        exp = sorted(tuple(map(str, row)) for row in Mexp)
        assert got == exp


@pytest.mark.parametrize("seed", range(20))
def test_determinant_matches_independent_fraction_and_mpmath(seed):
    rng = random.Random(31000 + seed)
    n = rng.randint(1, 5)
    A = [
        [Fraction(rng.randint(-5, 5), rng.randint(1, 3)) for _ in range(n)]
        for _ in range(n)
    ]
    b = [[Fraction(0)] for _ in range(n)]
    rows, mults = to_integer_system(A, b)
    res = kernel.eliminate(rows, n, input_row_multipliers=mults)
    if res.rank < n:
        assert oracle.det_fraction(A) == 0
        assert res.determinant_original is None
        return
    expected = oracle.det_fraction(A)
    assert res.determinant_original == expected
    # third-party high-precision cross-check (relative agreement)
    det_mp = oracle.mpmath_determinant(A)
    import mpmath
    ratio = abs(det_mp / mpmath.mpf(str(expected)))
    assert abs(ratio - 1) < mpmath.mpf("1e-60")


# ---------------------------------------------------------------------------
# Budget behavior
# ---------------------------------------------------------------------------

def test_budget_hook_receives_pre_step_snapshot():
    seen: list[kernel.ProgressSnapshot] = []
    kernel.eliminate([[1, 0, 1], [0, 1, 2]], 2, budget_hook=seen.append)
    assert seen[0].step == 0 and seen[0].column == -1
    assert seen[0].matrix == [["1", "0", "1"], ["0", "1", "2"]]


def test_budget_exhausted_at_step_zero_carries_input_snapshot():
    def hook(snap):
        if snap.step == 0:
            raise BudgetExhausted("stop", {"progress": snap.to_dict()})

    with pytest.raises(BudgetExhausted) as ei:
        kernel.eliminate([[123456, 1]], 1, budget_hook=hook)
    p = ei.value.details["progress"]
    assert p["matrix"] == [["123456", "1"]]
    assert p["pivots"] == [] and p["swaps"] == []


def test_budget_exhausted_midway_records_pivots_swaps_and_matrix():
    # Hilbert 6x6 blows past a 4-digit budget during forward elimination.
    n = 8
    A = [[Fraction(1, i + j + 1) for j in range(n)] for i in range(n)]
    b = [[Fraction(1, i + 1)] for i in range(n)]
    rows, mults = to_integer_system(A, b)

    def hook(snap):
        if snap.max_decimal_digits > 6:
            raise BudgetExhausted("digit budget", {"progress": snap.to_dict()})

    with pytest.raises(BudgetExhausted) as ei:
        kernel.eliminate(rows, n, budget_hook=hook, input_row_multipliers=mults)
    p = ei.value.details["progress"]
    assert p["phase"] == "forward"
    assert p["step"] >= 1
    assert len(p["pivots"]) == p["step"]
    assert isinstance(p["swaps"], list)
    # the snapshot is exact and replayable: same number of rows, all int strings
    assert len(p["matrix"]) == n
    for row in p["matrix"]:
        for v in row:
            int(v)  # raises if not an exact integer literal


def test_exact_division_violation_is_computation_failure(monkeypatch):
    # Corrupt one intermediate update by forcing a bad divisor: call the
    # internal exact_div directly to confirm the error class.
    with pytest.raises(ComputationFailed) as ei:
        kernel._exact_div(7, 3)
    assert ei.value.category.value == "computation_failed"
    assert ei.value.details["remainder"] == "1"


def test_bad_multipliers_rejected():
    with pytest.raises(ComputationFailed):
        kernel.eliminate([[1, 1]], 1, input_row_multipliers=[0])
