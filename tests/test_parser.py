"""Tests for the restricted rule language: parsing and explicit rejection."""

from __future__ import annotations

import pytest

from min_iowl.lang import ast
from min_iowl.lang.errors import ErrorCode, LangError
from min_iowl.lang.parser import (
    axiom_from_json,
    class_expr_from_json,
    parse_axiom,
    parse_axioms,
)


# ---------------------------------------------------------------- parsing ---
def test_parse_subclass_chain_functional() -> None:
    ax = parse_axiom("SubClassOf(Dog Animal)")
    assert isinstance(ax, ast.SubClassOf)
    assert isinstance(ax.sub, ast.ClassName) and ax.sub.name == "Dog"
    assert isinstance(ax.sup, ast.ClassName) and ax.sup.name == "Animal"


def test_parse_intersection_flattens_and_dedups() -> None:
    ax = parse_axiom(
        "SubClassOf(ObjectIntersectionOf(A ObjectIntersectionOf(B A) B) C)"
    )
    assert isinstance(ax.sub, ast.ObjectIntersection)
    names = tuple(op.name for op in ax.sub.operands)  # type: ignore[union-attr]
    assert names == ("A", "B")


def test_parse_equivalence_ring_and_assertion() -> None:
    axioms = parse_axioms(
        "EquivalentClasses(HomoSapiens Human Person)\n"
        "ClassAssertion(Person alice)"
    )
    assert len(axioms) == 2
    assert isinstance(axioms[0], ast.EquivalentClasses)
    assert len(axioms[0].operands) == 3
    assert isinstance(axioms[1], ast.ClassAssertion)
    assert axioms[1].individual == "alice"


def test_json_constructor_equivalent_to_functional() -> None:
    from_json = axiom_from_json(
        {
            "type": "SubClassOf",
            "sub": {"type": "ObjectIntersectionOf",
                    "operands": [{"type": "Class", "name": "A"},
                                 {"type": "Class", "name": "B"}]},
            "sup": {"type": "Class", "name": "C"},
        }
    )
    from_text = parse_axiom("SubClassOf(ObjectIntersectionOf(A B) C)")
    assert from_json == from_text


def test_curie_names_with_colon_are_class_names() -> None:
    expr = class_expr_from_json({"type": "Class", "name": "ex:Dog"})
    assert isinstance(expr, ast.ClassName) and expr.name == "ex:Dog"


# ------------------------------------------------------- explicit rejection --
@pytest.mark.parametrize(
    "text",
    [
        "SubClassOf(A ObjectSomeValuesFrom(hasPart B))",
        "SubClassOf(ObjectUnionOf(A B) C)",
        "SubClassOf(ObjectComplementOf(A) B)",
        "SubClassOf(A ObjectMinCardinality(2 hasPart B))",
    ],
)
def test_unsupported_functional_constructors_rejected_with_position(text: str) -> None:
    with pytest.raises(LangError) as exc:
        parse_axiom(text)
    assert exc.value.code == ErrorCode.UNSUPPORTED_CONSTRUCTOR
    # rejected rather than silently accepted: a position pinpoints the node
    assert exc.value.position


def test_unsupported_json_constructor_rejected_not_labelled() -> None:
    with pytest.raises(LangError) as exc:
        axiom_from_json(
            {
                "type": "SubClassOf",
                "sub": {
                    "type": "ObjectSomeValuesFrom",
                    "property": {"type": "ObjectProperty", "name": "hasPet"},
                    "filler": {"type": "Class", "name": "Cat"},
                },
                "sup": {"type": "Class", "name": "PetOwner"},
            }
        )
    assert exc.value.code == ErrorCode.UNSUPPORTED_CONSTRUCTOR
    assert "hasPet" not in exc.value.message  # property must not become a label


def test_unsupported_axiom_head_rejected() -> None:
    with pytest.raises(LangError) as exc:
        parse_axiom("FunctionalObjectProperty(hasPart)")
    assert exc.value.code == ErrorCode.UNSUPPORTED_CONSTRUCTOR


# -------------------------------------------------------------- malformed ---
def test_disjoint_needs_two_operands() -> None:
    with pytest.raises(LangError) as exc:
        parse_axiom("DisjointClasses(A)")
    assert exc.value.code == ErrorCode.MALFORMED_EXPRESSION


def test_intersection_needs_two_operands_json() -> None:
    with pytest.raises(LangError) as exc:
        class_expr_from_json(
            {"type": "ObjectIntersectionOf", "operands": [{"type": "Class", "name": "A"}]}
        )
    assert exc.value.code == ErrorCode.MALFORMED_EXPRESSION


def test_unknown_type_is_malformed_not_a_label() -> None:
    with pytest.raises(LangError) as exc:
        axiom_from_json({"type": "SubClassOf",
                         "sub": {"type": "Class", "name": "A"},
                         "sup": {"type": "Bogus", "name": "B"}})
    assert exc.value.code == ErrorCode.MALFORMED_EXPRESSION


def test_unbalanced_parentheses() -> None:
    with pytest.raises(LangError) as exc:
        parse_axiom("SubClassOf(A B")
    assert exc.value.code == ErrorCode.MALFORMED_EXPRESSION
