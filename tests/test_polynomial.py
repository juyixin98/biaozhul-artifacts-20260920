"""Tests for the N[X] polynomial semantic domain.

These hand-computed expectations are written independently of the operator
implementations; they pin the ring laws the whole service relies on.
"""
from __future__ import annotations

from fractions import Fraction

import pytest

from provenance.errors import WeightError
from provenance.polynomial import Poly, add, mono, mul, sum_polys


def test_variable_and_constant_rendering():
    assert Poly.var("x").to_string() == "x"
    assert Poly.one().to_string() == "1"
    assert Poly.zero().to_string() == "0"
    assert Poly({mono("x", "y"): 3}).to_string() == "3*x*y"


def test_duplicate_contributions_add_instead_of_collapsing():
    # The crux vs. a set-of-ids: two derivations through the same tuple are 2x.
    twice = Poly.var("x") + Poly.var("x")
    assert twice == Poly({mono("x"): 2})
    assert twice.to_string() == "2*x"


def test_joint_dependencies_multiply():
    poly = Poly.var("x") * Poly.var("y")
    assert poly == Poly({mono("x", "y"): 1})
    assert poly.to_string() == "x*y"


def test_self_pairing_yields_squared_variable():
    # A self-join pairing a tuple t with itself is x*x = x**2, not x.
    squared = Poly.var("x") * Poly.var("x")
    assert squared == Poly({mono("x", "x"): 1})
    assert squared.to_string() == "x**2"


def test_normalisation_merges_like_terms_and_drops_zero():
    poly = Poly(
        [
            (mono("a", "b"), 1),
            (mono("b", "a"), 2),  # same monomial, different construction order
            (mono("c"), 0),       # zero coefficient disappears
        ]
    )
    assert poly == Poly({mono("a", "b"): 3})


def test_normalisation_does_not_change_semantics_under_eval():
    raw = add(mul(Poly.var("x"), Poly.var("y")), Poly.var("x"))
    weights = {"x": Fraction(2, 3), "y": Fraction(7, 5)}
    # Hand value: x*y + x = 14/15 + 2/3 = 14/15 + 10/15 = 24/15 = 8/5
    assert raw.evaluate(weights) == Fraction(8, 5)


def test_sum_of_distinct_derivations():
    poly = sum_polys([Poly.var("x"), Poly.var("y"), Poly.var("x")])
    assert poly == Poly([(mono("x"), 2), (mono("y"), 1)])


def test_evaluate_squared_term_uses_exponent():
    weights = {"x": Fraction(3)}
    assert Poly.var("x").evaluate(weights) == 3
    assert (Poly.var("x") * Poly.var("x")).evaluate(weights) == 9


def test_evaluate_missing_weight_is_typed_error():
    with pytest.raises(WeightError) as exc:
        Poly.var("x").evaluate({})
    assert exc.value.category == "rejected_weights"
    assert exc.value.details["variable"] == "x"


def test_evaluate_rejects_non_numeric_weight():
    with pytest.raises(WeightError) as exc:
        Poly.var("x").evaluate({"x": "not-a-number"})
    assert exc.value.category == "rejected_weights"


def test_absorbing_zero_and_identity_one():
    x = Poly.var("x")
    assert (x * Poly.zero()).is_zero
    assert (x + Poly.zero()) == x
    assert x * Poly.one() == x


def test_negative_coefficient_rejected():
    with pytest.raises(ValueError):
        Poly({mono("x"): -1})
