"""Hand-computed verification of every metric, plus zero-denominator semantics.

Expected values come from tests/fixtures/expected_metrics.json (fractions
worked out on paper) and are independently recomputed by tests/reference.py;
they are never produced by app.mining itself.
"""
from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict, List, Tuple

import pytest

from app.mining import compute_metrics
from tests.reference import exact_metrics, normalized_transactions


def _f(value: float | None) -> Fraction | None:
    return None if value is None else Fraction(value).limit_denominator(10_000)


def test_support_confidence_lift_leverage_match_hand_computed_fractions(
    raw_transactions: List[List[str]], expected_metrics_data: Dict[str, Any]
) -> None:
    txns = normalized_transactions(raw_transactions)
    n = expected_metrics_data["n_transactions"]
    assert len(txns) == n

    for rule_name, expected in expected_metrics_data["rules"].items():
        ant_str, cons_str = rule_name.split("=>")
        a = [x for x in ant_str.split(",") if x]
        c = [x for x in cons_str.split(",") if x]

        m = compute_metrics(n, expected["union"], expected["antecedent"], expected["consequent"])

        assert _f(m.support) == Fraction(*expected["support"]), rule_name
        assert _f(m.confidence) == Fraction(*expected["confidence"]), rule_name
        assert _f(m.lift) == Fraction(*expected["lift"]), rule_name
        assert _f(m.leverage) == Fraction(*expected["leverage"]), rule_name
        assert m.defined is True, rule_name

        # Independent brute-force oracle recomputation from raw rows.
        oracle = exact_metrics(txns, a, c)
        assert _f(m.support) == oracle["support"], rule_name
        assert _f(m.confidence) == oracle["confidence"], rule_name
        assert _f(m.lift) == oracle["lift"], rule_name
        assert _f(m.leverage) == oracle["leverage"], rule_name
        assert m.support_count == oracle["n_u"]
        assert m.antecedent_count == oracle["n_a"]
        assert m.consequent_count == oracle["n_c"]


def test_mutually_exclusive_pair_has_zero_lift_and_negative_leverage() -> None:
    # c and d never co-occur in the canonical corpus: |c|=3, |d|=2, n=5.
    m = compute_metrics(5, 0, 3, 2)
    assert m.support_count == 0
    assert m.confidence == 0.0
    assert m.lift == 0.0
    assert _f(m.leverage) == Fraction(-6, 25)
    assert m.defined is True  # zero numerator is defined; it is zero support


def test_zero_antecedent_is_undefined_not_infinity() -> None:
    m = compute_metrics(5, 0, 0, 3)
    assert m.confidence is None
    assert m.lift is None
    assert m.defined is False
    assert m.undefined_reason is not None and "antecedent support count is 0" in m.undefined_reason
    assert m.confidence_denominator == 0
    assert m.lift_denominator == 0.0
    # Leverage stays defined: 0 - 0 * (3/5) = 0.
    assert m.leverage == 0.0


def test_zero_consequent_undefined_lift_but_confidence_is_zero() -> None:
    m = compute_metrics(5, 0, 3, 0)
    assert m.confidence == 0.0
    assert m.lift is None
    assert m.defined is False
    assert "consequent support count is 0" in (m.undefined_reason or "")
    assert m.leverage == 0.0


def test_both_sides_zero_is_undefined() -> None:
    m = compute_metrics(5, 0, 0, 0)
    assert m.confidence is None
    assert m.lift is None
    assert m.defined is False
    assert m.leverage == 0.0


def test_non_positive_n_rejected() -> None:
    with pytest.raises(ValueError):
        compute_metrics(0, 0, 0, 0)


@pytest.mark.parametrize(
    "counts,expected_lift_fraction",
    [
        ((3, 3, 4), Fraction(5, 4)),   # b -> a
        ((0, 3, 2), Fraction(0, 1)),   # c -> d, mutually exclusive
        ((1, 4, 2), Fraction(5, 8)),   # a -> e, rare and < 1
    ],
)
def test_lift_formula_exact(counts: Tuple[int, int, int], expected_lift_fraction: Fraction) -> None:
    union, ant, cons = counts
    m = compute_metrics(5, union, ant, cons)
    assert _f(m.lift) == expected_lift_fraction
