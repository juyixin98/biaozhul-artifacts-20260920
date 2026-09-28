"""Tests for the rule-language model validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.solver import (
    BinaryConstraint,
    BinaryRelation,
    CSPModel,
    ComparisonOp,
    RelationKind,
)


def valid_relation() -> BinaryRelation:
    return BinaryRelation(kind=RelationKind.COMPARISON, op=ComparisonOp.LT)


def test_valid_model_accepted() -> None:
    model = CSPModel(
        name="ok",
        domains={"x": [1, 2], "y": [1, 2]},
        binary_constraints=[
            BinaryConstraint(left="x", right="y", relation=valid_relation())
        ],
        all_different=[["x", "y"]],
    )
    assert model.variable_count() == 2
    assert model.constraint_count() == 2


def test_empty_domain_rejected() -> None:
    with pytest.raises(ValidationError, match="empty domain"):
        CSPModel(name="bad", domains={"x": []})


def test_duplicate_domain_values_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        CSPModel(name="bad", domains={"x": [1, 1, 2]})


def test_unknown_variable_in_constraint_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown variable"):
        CSPModel(
            name="bad",
            domains={"x": [1]},
            binary_constraints=[
                BinaryConstraint(left="x", right="ghost", relation=valid_relation())
            ],
        )


def test_self_constraint_rejected() -> None:
    with pytest.raises(ValidationError, match="distinct variables"):
        CSPModel(
            name="bad",
            domains={"x": [1, 2]},
            binary_constraints=[
                BinaryConstraint(left="x", right="x", relation=valid_relation())
            ],
        )


def test_all_different_duplicates_and_unknowns_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicates"):
        CSPModel(name="bad", domains={"x": [1]}, all_different=[["x", "x"]])
    with pytest.raises(ValidationError, match="unknown variable"):
        CSPModel(name="bad", domains={"x": [1]}, all_different=[["x", "ghost"]])


def test_comparison_relation_requires_op_and_no_pairs() -> None:
    with pytest.raises(ValidationError):
        BinaryRelation(kind=RelationKind.COMPARISON)
    with pytest.raises(ValidationError):
        BinaryRelation(
            kind=RelationKind.COMPARISON,
            op=ComparisonOp.EQ,
            pairs=frozenset({(1, 1)}),
        )


def test_relation_holds_semantics() -> None:
    allowed = BinaryRelation(
        kind=RelationKind.ALLOWED, pairs=frozenset({(1, 1), (2, 3)})
    )
    assert allowed.holds(1, 1)
    assert not allowed.holds(1, 2)

    forbidden = BinaryRelation(
        kind=RelationKind.FORBIDDEN, pairs=frozenset({(1, 1)})
    )
    assert forbidden.holds(1, 2)
    assert not forbidden.holds(1, 1)
