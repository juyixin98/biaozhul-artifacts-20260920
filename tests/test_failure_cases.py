"""Non-SPD / singular fixtures: failure category and pivot location."""
from __future__ import annotations

import numpy as np
import pytest

from app.errors import ErrorCode
from app.numerical_input.fixtures import (
    indefinite_3x3, negative_diagonal, singular_matrix)


def test_negative_diagonal_reported_with_original_index(engine):
    fx = negative_diagonal(n=8, bad=3)
    with pytest.raises(Exception) as ei:
        engine.solve(fx.n, fx.rows, fx.cols, fx.vals, fx.rhs,
                     ordering="natural")
    exc = ei.value
    assert exc.code == ErrorCode.NON_SPD_PIVOT
    assert exc.original_index == 3
    assert exc.pivot_value < 0


def test_indefinite_pivot_value_is_minus_three(engine):
    fx = indefinite_3x3()
    with pytest.raises(Exception) as ei:
        engine.solve(fx.n, fx.rows, fx.cols, fx.vals, fx.rhs,
                     ordering="natural")
    exc = ei.value
    assert exc.code == ErrorCode.NON_SPD_PIVOT
    # Schur complement at pivot position 1: 1 - 2*2/1 = -3 exactly
    assert exc.pivot == 1
    assert exc.original_index == 1
    assert exc.pivot_value == pytest.approx(-3.0, abs=1e-12)


def test_singular_zero_pivot_classified_separately(engine):
    fx = singular_matrix(n=6, zero_at=4)
    with pytest.raises(Exception) as ei:
        engine.solve(fx.n, fx.rows, fx.cols, fx.vals, fx.rhs,
                     ordering="natural")
    exc = ei.value
    assert exc.code == ErrorCode.SINGULAR_PIVOT
    assert exc.original_index == 4
    assert abs(exc.pivot_value) <= 1e-11


def test_negative_pivot_diagonal_matrix():
    # pure diagonal [-1] -> fail at first pivot, value -1
    from app.service.engine import FactorizationEngine
    eng = FactorizationEngine(enable_cache=False)
    with pytest.raises(Exception) as ei:
        eng.solve(1, [0], [0], [-1.0], [1.0], ordering="natural")
    assert ei.value.code == ErrorCode.NON_SPD_PIVOT
    assert ei.value.pivot_value == pytest.approx(-1.0)
