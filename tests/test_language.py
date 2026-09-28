"""Tests for the relational query rule language: parse + schema validation."""
import pytest

from app.provenance.language import (
    QueryValidationError,
    parse_query,
)

SCHEMA = {
    "R": ("a", "b"),
    "S": ("b", "c"),
    "T": ("a", "b"),
}


def test_relation_scan_defaults_to_qualified_labels():
    node = parse_query({"op": "relation", "relation": "R"}, SCHEMA)
    assert node.output_labels == ("R.a", "R.b")


def test_self_join_with_aliases_resolves():
    query = {
        "op": "join",
        "left": {"op": "relation", "relation": "R", "alias": "l"},
        "right": {"op": "relation", "relation": "R", "alias": "r"},
        "on": [["l.b", "r.b"]],
    }
    node = parse_query(query, SCHEMA)
    assert node.output_labels == ("l.a", "l.b", "r.a", "r.b")


def test_select_then_project_labels():
    query = {
        "op": "project",
        "columns": ["R.a"],
        "input": {
            "op": "select",
            "condition": {"col": "R.b", "op": "=", "value": 1},
            "input": {"op": "relation", "relation": "R"},
        },
    }
    node = parse_query(query, SCHEMA)
    assert node.output_labels == ("R.a",)


def test_bare_column_resolves_when_unambiguous():
    query = {
        "op": "select",
        "condition": {"col": "b", "op": ">", "value": 0},
        "input": {"op": "relation", "relation": "R"},
    }
    node = parse_query(query, SCHEMA)
    assert node.output_labels == ("R.a", "R.b")


def test_bare_column_ambiguous_after_join_is_rejected():
    query = {
        "op": "select",
        "condition": {"col": "b", "op": "=", "value": 1},
        "input": {
            "op": "join",
            "left": {"op": "relation", "relation": "R", "alias": "l"},
            "right": {"op": "relation", "relation": "S", "alias": "r"},
            "on": [["l.b", "r.b"]],
        },
    }
    with pytest.raises(QueryValidationError) as exc:
        parse_query(query, SCHEMA)
    assert exc.value.category == "AMBIGUOUS_COLUMN"


def test_unknown_relation_and_column_are_named_categories():
    with pytest.raises(QueryValidationError) as exc:
        parse_query({"op": "relation", "relation": "ghost"}, SCHEMA)
    assert exc.value.category == "UNKNOWN_RELATION"

    query = {
        "op": "select",
        "condition": {"col": "R.nope", "op": "=", "value": 1},
        "input": {"op": "relation", "relation": "R"},
    }
    with pytest.raises(QueryValidationError) as exc:
        parse_query(query, SCHEMA)
    assert exc.value.category == "UNKNOWN_COLUMN"


def test_comparison_against_null_literal_is_rejected_with_null_category():
    query = {
        "op": "select",
        "condition": {"col": "R.b", "op": "=", "value": None},
        "input": {"op": "relation", "relation": "R"},
    }
    with pytest.raises(QueryValidationError) as exc:
        parse_query(query, SCHEMA)
    assert exc.value.category == "NULL_PREDICATE_NOT_SUPPORTED"


def test_is_null_predicate_is_accepted():
    query = {
        "op": "select",
        "condition": {"col": "R.b", "op": "is_null"},
        "input": {"op": "relation", "relation": "R"},
    }
    node = parse_query(query, SCHEMA)
    assert node.output_labels == ("R.a", "R.b")


def test_unknown_operator_rejected():
    query = {
        "op": "select",
        "condition": {"col": "R.b", "op": "~~", "value": 1},
        "input": {"op": "relation", "relation": "R"},
    }
    with pytest.raises(QueryValidationError) as exc:
        parse_query(query, SCHEMA)
    assert exc.value.category == "UNSUPPORTED_OPERATOR"


def test_malformed_query_rejected():
    with pytest.raises(QueryValidationError) as exc:
        parse_query({"op": "select"}, SCHEMA)
    assert exc.value.category == "MALFORMED_QUERY"


def test_union_requires_matching_arity_and_names():
    ok = {
        "op": "union",
        "left": {"op": "relation", "relation": "R"},
        "right": {"op": "relation", "relation": "T"},
    }
    node = parse_query(ok, SCHEMA)
    assert node.output_labels == ("R.a", "R.b") or node.output_labels == ("T.a", "T.b")

    bad = {
        "op": "union",
        "left": {"op": "relation", "relation": "R"},
        "right": {"op": "relation", "relation": "S"},
    }
    with pytest.raises(QueryValidationError) as exc:
        parse_query(bad, SCHEMA)
    assert exc.value.category == "UNION_SCHEMA_MISMATCH"


def test_projection_duplicate_and_unknown_rejected():
    with pytest.raises(QueryValidationError) as exc:
        parse_query(
            {"op": "project", "columns": ["R.a", "R.a"],
             "input": {"op": "relation", "relation": "R"}},
            SCHEMA,
        )
    assert exc.value.category == "DUPLICATE_OUTPUT_COLUMN"

    with pytest.raises(QueryValidationError) as exc:
        parse_query(
            {"op": "project", "columns": ["R.z"],
             "input": {"op": "relation", "relation": "R"}},
            SCHEMA,
        )
    assert exc.value.category == "UNKNOWN_COLUMN"


def test_join_requires_on_clause_and_known_columns():
    with pytest.raises(QueryValidationError) as exc:
        parse_query(
            {
                "op": "join",
                "left": {"op": "relation", "relation": "R"},
                "right": {"op": "relation", "relation": "S"},
            },
            SCHEMA,
        )
    assert exc.value.category == "MALFORMED_QUERY"

    with pytest.raises(QueryValidationError) as exc:
        parse_query(
            {
                "op": "join",
                "left": {"op": "relation", "relation": "R"},
                "right": {"op": "relation", "relation": "S"},
                "on": [["R.b", "S.q"]],
            },
            SCHEMA,
        )
    assert exc.value.category == "UNKNOWN_COLUMN"


def test_unknown_root_op():
    with pytest.raises(QueryValidationError) as exc:
        parse_query({"op": "difference", "left": {}, "right": {}}, SCHEMA)
    assert exc.value.category == "UNSUPPORTED_OPERATOR"
