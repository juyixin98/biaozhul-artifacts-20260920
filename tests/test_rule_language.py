"""Parser tests: well-formedness and concrete failure categories."""
from __future__ import annotations

import pytest

from provenance.errors import PlanError
from provenance.rule_language import Join, Project, RelationRef, Select, Union, parse_plan


def test_parse_relation_requires_version():
    with pytest.raises(PlanError) as exc:
        parse_plan({"op": "relation", "name": "edge"})
    assert exc.value.category == "rejected_plan"


def test_parse_select_with_literal():
    node = parse_plan(
        {
            "op": "select",
            "child": {"op": "relation", "name": "edge", "version": "v1"},
            "predicates": [{"op": "=", "left": "src", "literal": "a"}],
        }
    )
    assert isinstance(node, Select)
    assert isinstance(node.child, RelationRef)
    assert node.predicates[0].literal == "a"


def test_parse_join_requires_column_predicate():
    plan = {
        "op": "join",
        "left": {"op": "relation", "name": "edge", "version": "v1"},
        "right": {"op": "relation", "name": "redge", "version": "v1"},
        "predicates": [{"op": "=", "left": "dst", "literal": "x"}],
    }
    with pytest.raises(PlanError) as exc:
        parse_plan(plan)
    assert exc.value.category == "rejected_plan"


def test_parse_project_rejects_duplicate_columns():
    with pytest.raises(PlanError):
        parse_plan(
            {
                "op": "project",
                "child": {"op": "relation", "name": "edge", "version": "v1"},
                "columns": ["src", "src"],
            }
        )


def test_parse_unknown_operator_names_failure_class():
    with pytest.raises(PlanError) as exc:
        parse_plan({"op": "difference", "left": {}, "right": {}})
    assert exc.value.category == "rejected_plan"
    assert "difference" in str(exc.value)


def test_parse_bad_comparison_op():
    with pytest.raises(PlanError):
        parse_plan(
            {
                "op": "select",
                "child": {"op": "relation", "name": "edge", "version": "v1"},
                "predicates": [{"op": "~~", "left": "src", "literal": "a"}],
            }
        )


def test_parse_self_join_aliases():
    node = parse_plan(
        {
            "op": "join",
            "left": {"op": "relation", "name": "edge", "version": "v1", "alias": "e1"},
            "right": {"op": "relation", "name": "edge", "version": "v1", "alias": "e2"},
            "predicates": [{"op": "=", "left": "e1.dst", "right": "e2.src"}],
        }
    )
    assert isinstance(node, Join)
    assert node.left.binding == "e1"
    assert node.right.binding == "e2"


def test_union_distinct_rejected():
    with pytest.raises(PlanError):
        parse_plan(
            {
                "op": "union",
                "distinct": True,
                "left": {"op": "relation", "name": "edge", "version": "v1"},
                "right": {"op": "relation", "name": "redge", "version": "v1"},
            }
        )


def test_node_types_round_trip():
    node = parse_plan(
        {
            "op": "project",
            "columns": ["src"],
            "child": {
                "op": "union",
                "left": {"op": "relation", "name": "edge", "version": "v1"},
                "right": {"op": "relation", "name": "redge", "version": "v1"},
            },
        }
    )
    assert isinstance(node, Project)
    assert isinstance(node.child, Union)
