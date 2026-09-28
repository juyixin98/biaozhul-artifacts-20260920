"""Tests for the N[X] provenance polynomial semiring.

Reference values here are hand-computed; they do NOT call the production
normalizer to generate expected answers.
"""
import math

import pytest

from app.provenance.expressions import (
    Polynomial,
    MissingWeightError,
)


def test_zero_and_one_constants():
    assert Polynomial.zero().terms == {}
    assert Polynomial.one().terms == {(): 1}


def test_variable_is_singleton_monomial():
    assert Polynomial.var("x1").terms == {("x1",): 1}


def test_add_collects_like_terms():
    # duplicate-row contributions: x + x = 2x
    p = Polynomial.var("x") + Polynomial.var("x")
    assert p.terms == {("x",): 2}


def test_add_of_distinct_witnesses_stays_separate():
    # union duplicates come from different input rows: x + y
    p = Polynomial.var("x") + Polynomial.var("y")
    assert p.terms == {("x",): 1, ("y",): 1}


def test_multiply_concatenates_witnesses():
    # joint dependencies multiply: (x + y) * z = xz + yz
    p = (Polynomial.var("x") + Polynomial.var("y")) * Polynomial.var("z")
    assert p.terms == {("x", "z"): 1, ("y", "z"): 1}


def test_self_join_keeps_exponent():
    # R(x) joined with itself on equal keys -> x * x = x^2 (NOT collapsed to x)
    p = Polynomial.var("x") * Polynomial.var("x")
    assert p.terms == {("x", "x"): 1}
    weights = {"x": 3}
    assert p.evaluate(weights) == 9


def test_distributive_law_alternative_derivation():
    # Same query two ways; results must be identical polynomials.
    x, y, z = Polynomial.var("x"), Polynomial.var("y"), Polynomial.var("z")
    left = (x + y) * z
    right = x * z + y * z
    assert left.terms == right.terms


def test_absorbing_and_identity():
    x = Polynomial.var("x")
    assert (x + Polynomial.zero()).terms == x.terms
    assert (x * Polynomial.one()).terms == x.terms
    assert (x * Polynomial.zero()).terms == {}


def test_hand_computed_three_row_join_polynomial():
    # R: r1(a,1)=w_r1, r2(a,2)=w_r2 ; S: s1(1,x)=w_s1, s2(2,y)=w_s2
    # join on B: (a,1,x): r1*s1 ; (a,2,y): r2*s2
    r1s1 = Polynomial.var("r1") * Polynomial.var("s1")
    r2s2 = Polynomial.var("r2") * Polynomial.var("s2")
    result = r1s1 + r2s2
    assert result.terms == {
        ("r1", "s1"): 1,
        ("r2", "s2"): 1,
    }
    # inject numeric weights
    weights = {"r1": 2, "r2": 3, "s1": 5, "s2": 7}
    assert result.evaluate(weights) == 2 * 5 + 3 * 7  # 31


def test_evaluate_with_duplicate_contributions():
    # 2x + 3y with x=4, y=10 -> 38
    p = (Polynomial.var("x") + Polynomial.var("x")) + (
        Polynomial.var("y") + Polynomial.var("y") + Polynomial.var("y")
    )
    assert p.evaluate({"x": 4, "y": 10}) == 38


def test_evaluate_constant_polynomials():
    assert Polynomial.one().evaluate({}) == 1
    assert Polynomial.zero().evaluate({}) == 0


def test_missing_weight_is_named_error():
    with pytest.raises(MissingWeightError) as exc:
        Polynomial.var("r1").evaluate({})
    assert "r1" in str(exc.value)


def test_numeric_evaluation_accepts_floats():
    p = Polynomial.var("x") * Polynomial.var("y") + Polynomial.one()
    assert math.isclose(p.evaluate({"x": 1.5, "y": 2.0}), 4.0)


def test_render_is_canonical_and_stable():
    p = Polynomial.var("b") * Polynomial.var("a") + Polynomial.var("a") * Polynomial.var("a")
    rendered = p.render()
    assert rendered == "a^2 + a*b"
    # rendering twice is identical
    assert p.render() == rendered


def test_render_constants():
    assert Polynomial.zero().render() == "0"
    assert Polynomial.one().render() == "1"


def test_coefficients_are_always_canonical_after_add_and_mul():
    # (2x) * (3x + y) = 6x^2 + 2xy
    two_x = Polynomial.var("x") + Polynomial.var("x")
    three_x_plus_y = (
        Polynomial.var("x")
        + Polynomial.var("x")
        + Polynomial.var("x")
        + Polynomial.var("y")
    )
    p = two_x * three_x_plus_y
    assert p.terms == {("x", "x"): 6, ("x", "y"): 2}
