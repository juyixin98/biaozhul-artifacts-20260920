"""Runtime budget tests: Sturm evaluation and bisection depth guards."""

from __future__ import annotations

from fractions import Fraction as F

import pytest

from config.settings import BudgetConfig
from root_isolator.errors import ErrorCategory, IsolationError
from root_isolator.kernel.polynomial import RationalPoly
from root_isolator.kernel.isolate import isolate_roots


def test_sturm_pair_budget_is_enforced():
    budget = BudgetConfig(
        max_degree=64,
        max_coefficient_bits=4096,
        max_sturm_pairs=10,  # impossibly small
        max_bisection_depth=200,
        max_roots=256,
    )
    with pytest.raises(IsolationError) as exc:
        isolate_roots(RationalPoly([F(-1), F(0), F(1)]), budget)  # x^2-1
    assert exc.value.category is ErrorCategory.BUDGET_EXCEEDED
    assert exc.value.state["limit"] == 10


def test_bisection_depth_budget_is_inconclusive():
    budget = BudgetConfig(
        max_degree=64,
        max_coefficient_bits=4096,
        max_sturm_pairs=2_000_000,
        max_bisection_depth=2,  # too shallow to separate close roots
        max_roots=256,
    )
    gap = F(1, 2 ** 40)
    poly = RationalPoly([F(-1), F(1)]) * RationalPoly([F(-(1 + gap)), F(1)])
    with pytest.raises(IsolationError) as exc:
        isolate_roots(poly, budget)
    assert exc.value.category is ErrorCategory.INCONCLUSIVE
    assert exc.value.state["depth"] > 2


def test_too_many_roots_guard():
    budget = BudgetConfig(
        max_degree=64,
        max_coefficient_bits=4096,
        max_sturm_pairs=2_000_000,
        max_bisection_depth=200,
        max_roots=1,
    )
    # (x-1)(x+1) = x^2 - 1 has two distinct roots.
    with pytest.raises(IsolationError) as exc:
        isolate_roots(RationalPoly([F(-1), F(0), F(1)]), budget)
    assert exc.value.category is ErrorCategory.TOO_MANY_ROOTS
