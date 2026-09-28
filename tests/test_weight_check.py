"""Independent numeric-weight verification tests.

Weights are injected per input tuple and the symbolic polynomial evaluation is
compared against a separate numeric propagation implementation. Expected
numbers are hand computed; a tampering test proves the cross-check actually
rejects disagreement.
"""
from __future__ import annotations

from dataclasses import replace
from fractions import Fraction

import pytest

from provenance.polynomial import Poly, mono
from provenance.rule_language import parse_plan
from provenance.weight_check import coerce_weights, cross_check, evaluate_numeric
from provenance.errors import WeightError

TWO_HOP = {
    "op": "project",
    "columns": ["x.src", "y.dst"],
    "child": {
        "op": "join",
        "left": {"op": "relation", "name": "edge", "version": "v1", "alias": "x"},
        "right": {"op": "relation", "name": "edge", "version": "v1", "alias": "y"},
        "predicates": [{"op": "=", "left": "x.dst", "right": "y.src"}],
    },
}

# One distinct weight per input tuple (primes -> exact, easy hand checks).
RAW_WEIGHTS = {f"edge.e{i}": str(p) for i, p in enumerate([2, 3, 5, 7, 11, 13], start=1)}
# e1=2 e2=3 e3=5 e4=7 e5=11(NULL) e6=13(self loop)


def _weights():
    return coerce_weights(RAW_WEIGHTS)


def test_numeric_path_matches_symbolic_for_every_output(store, planner):
    result = planner.run(TWO_HOP, request_id="req-weight")
    numeric = evaluate_numeric(parse_plan(TWO_HOP), store, _weights(), "v1")
    mismatches = cross_check(result.rows, numeric, _weights())
    assert mismatches == []


def test_hand_computed_weighted_scores(store):
    numeric = evaluate_numeric(parse_plan(TWO_HOP), store, _weights(), "v1")
    # After projection the numeric key is already (x.src, y.dst).
    by_value = dict(numeric.rows)
    # (a,c): e1*e2 + e3*e2 = 2*3 + 5*3 = 6 + 15 = 21
    assert by_value[("a", "c")] == Fraction(21)
    # (b,d): e2*e4 = 3*7 = 21
    assert by_value[("b", "d")] == Fraction(21)
    # (c,d): e4*e6 = 7*13 = 91
    assert by_value[("c", "d")] == Fraction(91)
    # (d,d): e6*e6 = 13*13 = 169  <- squared variable weight
    assert by_value[("d", "d")] == Fraction(169)


def test_all_ones_weights_equal_multiplicity(store, planner):
    result = planner.run(TWO_HOP, request_id="req-ones")
    all_vars = {f"edge.e{i}": Fraction(1) for i in range(1, 7)}
    numeric = evaluate_numeric(parse_plan(TWO_HOP), store, all_vars, "v1")
    mismatches = cross_check(result.rows, numeric, all_vars)
    assert mismatches == []
    for row in result.rows:
        assert row.poly.evaluate(all_vars) == row.multiplicity


def test_cross_check_detects_tampered_polynomial(store, planner):
    result = planner.run(TWO_HOP, request_id="req-tamper")
    numeric = evaluate_numeric(parse_plan(TWO_HOP), store, _weights(), "v1")
    # Corrupt one row's polynomial: drop a derivation term.
    rows = list(result.rows)
    target = next(r for r in rows if r.values == {"src": "a", "dst": "c"})
    index = rows.index(target)
    tampered = replace(target, poly=Poly({mono("edge.e1", "edge.e2"): 1}))  # missing e3*e2
    rows[index] = tampered
    mismatches = cross_check(rows, numeric, _weights())
    assert len(mismatches) == 1
    assert mismatches[0].output == {"src": "a", "dst": "c"}
    # Symbolic now 6, independent numeric still 21.
    assert mismatches[0].symbolic == Fraction(6)
    assert mismatches[0].numeric == Fraction(21)


def test_missing_weight_is_typed_failure(store):
    with pytest.raises(WeightError) as exc:
        evaluate_numeric(parse_plan(TWO_HOP), store, {}, "v1")
    assert exc.value.category == "rejected_weights"


def test_coerce_weights_rejects_garbage():
    with pytest.raises(WeightError) as exc:
        coerce_weights({"edge.e1": "abc"})
    assert exc.value.category == "rejected_weights"


def test_coerce_weights_accepts_fractions():
    weights = coerce_weights({"edge.e1": "1/3", "edge.e2": 0.5})
    assert weights["edge.e1"] == Fraction(1, 3)
    assert weights["edge.e2"] == Fraction(1, 2)
