"""Unit tests for the integer-search primitive -- validated independently of
any distribution, using hand-built value functions.
"""
from __future__ import annotations

import math

import pytest

from sample_size_planner.estimation.integer_search import (
    continuous_threshold,
    smallest_integer_above,
    smallest_integer_with_backtrack,
)


def test_continuous_threshold_inverts_monotone_function() -> None:
    # f(x) = x^2 ; first x with x^2 >= 2 is sqrt(2).
    root = continuous_threshold(lambda x: x * x, 2.0, 0.0, 4.0)
    assert root == pytest.approx(math.sqrt(2), abs=1e-9)


def test_continuous_threshold_rejects_unbracketed_target() -> None:
    with pytest.raises(ValueError, match="not bracketed"):
        continuous_threshold(lambda x: x, 10.0, 0.0, 1.0)


def test_smallest_integer_above_witnesses_boundary() -> None:
    # step function that first reaches >= 0.8 at n = 12
    def value(n: int) -> float:
        return 0.05 + 0.0625 * n

    boundary = smallest_integer_above(value, 0.8, start=1, cap=1000)
    assert boundary.n == 12
    assert boundary.value_at_n == pytest.approx(0.8)
    assert boundary.value_at_n_minus_one == pytest.approx(0.7375)
    assert boundary.n_passes and boundary.n_minus_one_fails


def test_smallest_integer_above_raises_when_cap_binds() -> None:
    with pytest.raises(OverflowError, match="no integer <= 10"):
        smallest_integer_above(lambda n: 0.0, 0.9, start=1, cap=10)


def test_backtrack_recovers_from_warm_start_above_answer() -> None:
    # Exact binomial style: plateau from n=10..20 then rising. A warm start at
    # 18 must backtrack to the true minimal integer 10.
    def value(n: int) -> float:
        if n < 10:
            return 0.5
        if n <= 20:
            return 0.9      # plateau already above target 0.8
        return 0.95

    boundary = smallest_integer_with_backtrack(value, 0.8, start=18, cap=1000)
    assert boundary.n == 10
    assert boundary.value_at_n_minus_one == 0.5
    assert boundary.n_passes and boundary.n_minus_one_fails


def test_backtrack_ascends_when_start_belows_answer() -> None:
    boundary = smallest_integer_with_backtrack(
        lambda n: 0.05 + 0.0625 * n, 0.8, start=2, cap=1000)
    assert boundary.n == 12
    assert boundary.n_passes and boundary.n_minus_one_fails


def test_backtrack_halving_budget_escapes_long_plateau() -> None:
    # Plateau longer than the stepwise backtrack budget.
    def value(n: int) -> float:
        return 0.95 if n >= 100 else 0.2

    boundary = smallest_integer_with_backtrack(
        value, 0.9, start=5000, cap=10000, backtrack_budget=10)
    assert boundary.n == 100
    assert boundary.value_at_n_minus_one == 0.2
