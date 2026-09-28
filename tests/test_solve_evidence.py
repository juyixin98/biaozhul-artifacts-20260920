"""Unit tests for classification, parametric solutions and evidence."""

from __future__ import annotations

import random
from fractions import Fraction

import pytest

from rational_linalg.evidence import (
    fraction_free_determinant,
    verify_contradiction_witness,
    verify_left_nullspace,
    verify_solution,
)
from rational_linalg.solve import (
    INCONSISTENT,
    INFINITE,
    UNIQUE,
    rank_of,
    solve_augmented,
)

from .conftest import fixture_matrix, fixture_vector, load_fixture
from .independent_oracle import oracle_det, oracle_rank, oracle_solve


def _frac_vec(raw):
    return [Fraction(v) if isinstance(v, int) else Fraction(v) for v in raw]


# ---------- concrete fixture assertions (specific results) ----------

def test_big_common_factor_unique_small_answer():
    data = load_fixture("big_common_factor.json")
    A = fixture_matrix(data["A"])
    b = fixture_vector(data["b"])
    result = solve_augmented(A, b)
    assert result["classification"] is UNIQUE
    assert result["rank_a"] == 2
    assert result["particular"] == [Fraction(1), Fraction(2)]
    evidence = verify_solution(A, b, result["particular"], [])
    assert evidence["ok"] is True
    det = fraction_free_determinant(result["elimination"], 2)
    assert det == Fraction(10**20) * Fraction(10**20) * Fraction(-1)
    # sanity: det of [[1,2],[3,5]] scaled by 10^20 is (5-6)*10^40 = -10^40
    assert det == -Fraction(10**40)


def test_rank_deficient_infinite_parametric_substitution():
    data = load_fixture("rank_deficient_infinite.json")
    A = fixture_matrix(data["A"])
    b = fixture_vector(data["b"])
    result = solve_augmented(A, b)
    assert result["classification"] is INFINITE
    assert result["rank_a"] == 2
    assert result["free_columns"] == [2]
    assert len(result["null_basis"]) == 1
    assert result["null_basis"][0] == [Fraction(1), Fraction(-2), Fraction(1)]

    # Independently substitute particular, and three parameter values.
    for params in ([Fraction(0)], [Fraction(3)], [Fraction(-7, 2)]):
        check = verify_solution(
            A, b, result["particular"], result["null_basis"], params
        )
        assert check["parameterized_ok"] is True

    # A point NOT on the affine space must fail the independent check.
    bogus = [Fraction(0), Fraction(0), Fraction(0)]
    assert verify_solution(A, b, bogus, [])["ok"] is False


def test_inconsistent_pair_left_null_evidence():
    data = load_fixture("inconsistent_pair.json")
    A = fixture_matrix(data["A"])
    b = fixture_vector(data["b"])
    result = solve_augmented(A, b)
    assert result["classification"] is INCONSISTENT
    assert result["rank_a"] == 1
    assert result["rank_augmented"] == 2
    y = result["contradiction_witness"]
    evidence = verify_contradiction_witness(A, b, y)
    assert evidence["ok"] is True
    assert abs(evidence["yt_b"]) == Fraction(1)
    # The exact witness is proportional to (1, -1); sign must be preserved.
    assert y[0] == -y[1] and y[0] != 0
    assert evidence["yt_A"] == [Fraction(0), Fraction(0)]


def test_overdetermined_unique_rectangular():
    data = load_fixture("overdetermined_consistent.json")
    A = fixture_matrix(data["A"])
    b = fixture_vector(data["b"])
    result = solve_augmented(A, b)
    assert result["classification"] is UNIQUE
    assert result["particular"] == [Fraction(2), Fraction(-1)]
    rank_info = rank_of(A)
    assert rank_info["rank"] == 2
    assert rank_info["left_nullity"] == 1
    y = rank_info["left_nullspace"][0]
    assert verify_left_nullspace(A, [y])["ok"] is True
    # The dependency is row3 = row1 + row2, so witness is proportional to
    # (1, 1, -1).
    assert y[0] == y[1] == -y[2]


def test_underdetermined_two_directions_or_one_free_consistency():
    data = load_fixture("underdetermined.json")
    A = fixture_matrix(data["A"])
    b = fixture_vector(data["b"])
    result = solve_augmented(A, b)
    assert result["classification"] is INFINITE
    assert len(result["null_basis"]) == 1
    rng_params = [Fraction(-11, 3)]
    check = verify_solution(
        A, b, result["particular"], result["null_basis"], rng_params
    )
    assert check["parameterized_ok"] is True


def test_row_swap_determinant_and_negative_pivot_signs():
    data = load_fixture("row_swap_and_sign.json")
    A = fixture_matrix(data["A_antidiag"])
    b = fixture_vector(data["b_antidiag"])
    result = solve_augmented(A, b)
    assert result["row_swaps"] == [[1, 0]]
    assert result["particular"] == [Fraction(7), Fraction(3)]
    assert fraction_free_determinant(result["elimination"], 2) == -1

    A2 = fixture_matrix(data["A_neg"])
    b2 = fixture_vector(data["b_neg"])
    result2 = solve_augmented(A2, b2)
    assert result2["particular"] == [Fraction(0), Fraction(1)]
    assert fraction_free_determinant(result2["elimination"], 2) == -2
    assert result2["pivot_signs"][0] == "neg"


def test_near_float_indistinguishable_exact_answer():
    data = load_fixture("near_float_indistinguishable.json")
    A = fixture_matrix(data["A"])
    b = fixture_vector(data["b"])
    result = solve_augmented(A, b)
    assert result["classification"] is UNIQUE
    assert result["rank_a"] == 2
    assert result["particular"] == [Fraction(1), Fraction(1)]
    det = fraction_free_determinant(result["elimination"], 2)
    assert det == -1


# ---------- randomised cross-check vs the independent oracle ----------

def test_random_systems_match_independent_oracle():
    rng = random.Random(424242)
    for m, n in [(2, 2), (3, 3), (4, 3), (3, 4), (5, 5)]:
        for _ in range(40):
            A = [
                [Fraction(rng.randint(-6, 6)) for _ in range(n)]
                for _ in range(m)
            ]
            b = [Fraction(rng.randint(-6, 6)) for _ in range(m)]
            result = solve_augmented([row[:] for row in A], list(b))
            kind, payload = oracle_solve([row[:] for row in A], list(b))
            assert result["classification"] == kind, (m, n, A, b)
            if kind == UNIQUE:
                assert result["particular"] == payload
                assert verify_solution(A, b, result["particular"], [])["ok"]
            elif kind == INFINITE:
                particular, basis = payload
                # particular points may differ (same affine space): check both
                # solve the system and the null-space dimensions agree.
                check = verify_solution(
                    A, b, result["particular"], result["null_basis"]
                )
                assert check["ok"]
                assert len(result["null_basis"]) == len(basis)
            else:
                y = result["contradiction_witness"]
                assert verify_contradiction_witness(A, b, y)["ok"]


def test_random_consistent_systems_always_solvable():
    rng = random.Random(777)
    for m, n in [(3, 3), (4, 2), (2, 4)]:
        for _ in range(30):
            A, b, x_true = _oracle_consistent(rng, m, n)
            result = solve_augmented([row[:] for row in A], list(b))
            assert result["classification"] in (UNIQUE, INFINITE)
            assert verify_solution(
                A, b, result["particular"], result["null_basis"]
            )["ok"]


def _oracle_consistent(rng, m, n):
    from .independent_oracle import random_consistent_system

    return random_consistent_system(rng, m, n)


def test_random_rank_and_determinant_match_oracle():
    rng = random.Random(9001)
    for n in (1, 2, 3, 4):
        for _ in range(40):
            A = [
                [Fraction(rng.randint(-7, 7)) for _ in range(n)]
                for _ in range(n)
            ]
            info = rank_of([row[:] for row in A])
            assert info["rank"] == oracle_rank(A)
            assert verify_left_nullspace(A, info["left_nullspace"])["ok"]
            if info["rank"] == n:
                got = fraction_free_determinant(info["elimination"], n)
                assert got == oracle_det(A)
