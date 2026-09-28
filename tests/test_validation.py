"""Query validation: scope rejections carry stable failure categories."""
from __future__ import annotations

import pytest

from app.validation.queries import (
    EMPTY_ANTECEDENT,
    EMPTY_CONSEQUENT,
    OVERLAPPING_SIDES,
    THRESHOLD_OUT_OF_RANGE,
    QueryRejected,
    validate_rule_sides,
    validate_threshold,
)


def test_empty_antecedent_rejected():
    with pytest.raises(QueryRejected) as excinfo:
        validate_rule_sides([], ["beer"])
    assert excinfo.value.category == EMPTY_ANTECEDENT


def test_blank_only_antecedent_rejected():
    with pytest.raises(QueryRejected) as excinfo:
        validate_rule_sides(["", "  "], ["beer"])
    assert excinfo.value.category == EMPTY_ANTECEDENT


def test_empty_consequent_rejected():
    with pytest.raises(QueryRejected) as excinfo:
        validate_rule_sides(["beer"], [])
    assert excinfo.value.category == EMPTY_CONSEQUENT


def test_overlapping_sides_rejected():
    with pytest.raises(QueryRejected) as excinfo:
        validate_rule_sides(["beer", "milk"], ["milk", "bread"])
    assert excinfo.value.category == OVERLAPPING_SIDES


def test_valid_sides_normalized():
    q = validate_rule_sides([" beer ", "milk", "beer"], ["bread"])
    assert q.antecedent == frozenset({"beer", "milk"})
    assert q.consequent == frozenset({"bread"})


@pytest.mark.parametrize("value", [0.0, -0.5, 1.01, 2.0])
def test_threshold_out_of_range(value):
    with pytest.raises(QueryRejected) as excinfo:
        validate_threshold("min_confidence", value, lo=0.0, hi=1.0)
    assert excinfo.value.category == THRESHOLD_OUT_OF_RANGE


def test_threshold_boundary_one_is_allowed():
    assert validate_threshold("min_confidence", 1.0, lo=0.0, hi=1.0) == 1.0
