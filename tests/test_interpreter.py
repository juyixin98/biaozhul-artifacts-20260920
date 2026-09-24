"""Interpreter: operators, missing attributes (unknown), type safety."""

import pytest

from app.errors import EvaluationError, PolicyValidationError
from app.interpreter import AttrBag, eval_condition, validate_node
from app.logic import Tri


def bag(sub=None, res=None):
    return AttrBag(sub), AttrBag(res)


# ---------------------------------------------------------------- equality / in
def test_eq_scalars():
    s, r = bag({"role": "admin"}, {"kind": "doc"})
    node = {"op": "eq",
            "left": {"op": "attr", "bag": "subject", "key": "role"},
            "right": {"op": "lit", "value": "admin"}}
    assert eval_condition(node, s, r) is Tri.TRUE


def test_ne_and_false():
    s, r = bag({"role": "user"}, None)
    node = {"op": "ne",
            "left": {"op": "attr", "bag": "subject", "key": "role"},
            "right": {"op": "lit", "value": "admin"}}
    assert eval_condition(node, s, r) is Tri.TRUE


def test_in_set_membership():
    s, r = bag({"role": "auditor"}, None)
    node = {"op": "in",
            "left": {"op": "attr", "bag": "subject", "key": "role"},
            "right": {"op": "lit", "value": ["admin", "auditor"]}}
    assert eval_condition(node, s, r) is Tri.TRUE


def test_contains_and_subset():
    s, r = bag({"g": ["a", "b"]}, {"need": ["a"], "tags": ["x", "y"]})
    contains = {"op": "contains",
                "left": {"op": "attr", "bag": "subject", "key": "g"},
                "right": {"op": "lit", "value": "b"}}
    subset = {"op": "subset",
              "left": {"op": "attr", "bag": "resource", "key": "need"},
              "right": {"op": "attr", "bag": "subject", "key": "g"}}
    assert eval_condition(contains, s, r) is Tri.TRUE
    assert eval_condition(subset, s, r) is Tri.TRUE


def test_equality_is_family_aware():
    # 1 != True, "1" != 1 — no implicit coercion.
    s, r = bag({"n": 1}, None)
    for literal in (True, "1", 1.5):
        node = {"op": "eq",
                "left": {"op": "attr", "bag": "subject", "key": "n"},
                "right": {"op": "lit", "value": literal}}
        assert eval_condition(node, s, r) is Tri.FALSE


# ------------------------------------------------------------- missing -> unknown
def test_missing_attribute_is_unknown():
    s, r = bag({}, {})
    node = {"op": "eq",
            "left": {"op": "attr", "bag": "subject", "key": "clearance"},
            "right": {"op": "lit", "value": 5}}
    assert eval_condition(node, s, r) is Tri.UNKNOWN


def test_exists_distinguishes_missing():
    s, r = bag({"a": None}, None)
    exists = {"op": "exists", "bag": "subject", "key": "a"}
    missing = {"op": "exists", "bag": "subject", "key": "zzz"}
    assert eval_condition(exists, s, r) is Tri.TRUE
    assert eval_condition(missing, s, r) is Tri.FALSE


def test_missing_set_in_in_is_unknown_not_false():
    s, r = bag({"role": "x"}, None)
    node = {"op": "in",
            "left": {"op": "attr", "bag": "subject", "key": "role"},
            "right": {"op": "attr", "bag": "resource", "key": "allow_roles"}}
    assert eval_condition(node, s, r) is Tri.UNKNOWN


def test_kleene_composition_with_unknown():
    # false AND unknown -> false; true AND unknown -> unknown;
    # unknown OR true -> true; NOT unknown -> unknown
    s, r = bag({"a": False, "b": True}, None)
    def attr(k):
        return {"op": "attr", "bag": "subject", "key": k}
    assert eval_condition(
        {"op": "and", "args": [attr("a"), attr("missing")]}, s, r
    ) is Tri.FALSE
    assert eval_condition(
        {"op": "and", "args": [attr("b"), attr("missing")]}, s, r
    ) is Tri.UNKNOWN
    assert eval_condition(
        {"op": "or", "args": [attr("missing"), attr("b")]}, s, r
    ) is Tri.TRUE
    assert eval_condition(
        {"op": "not", "args": [attr("missing")]}, s, r
    ) is Tri.UNKNOWN


# ---------------------------------------------------------------- ordering / types
def test_ordering_compares_numbers_and_strings():
    s, r = bag({"c": 3}, None)
    node = {"op": "gte",
            "left": {"op": "attr", "bag": "subject", "key": "c"},
            "right": {"op": "lit", "value": 3}}
    assert eval_condition(node, s, r) is Tri.TRUE


def test_ordering_cross_family_is_error():
    s, r = bag({"c": 3}, None)
    node = {"op": "lt",
            "left": {"op": "attr", "bag": "subject", "key": "c"},
            "right": {"op": "lit", "value": "3"}}
    with pytest.raises(EvaluationError):
        eval_condition(node, s, r)


def test_in_requires_list_right():
    s, r = bag({"x": "a"}, {"allow": "a"})
    node = {"op": "in",
            "left": {"op": "attr", "bag": "subject", "key": "x"},
            "right": {"op": "attr", "bag": "resource", "key": "allow"}}
    with pytest.raises(EvaluationError):
        eval_condition(node, s, r)


def test_non_boolean_operand_is_error_not_truthiness():
    s, r = bag({"n": 5}, None)
    node = {"op": "and", "args": [
        {"op": "attr", "bag": "subject", "key": "n"}]}
    with pytest.raises(EvaluationError):
        eval_condition(node, s, r)


# ---------------------------------------------------------------- static validation
@pytest.mark.parametrize("node", [
    {"op": "eval", "code": "1"},                 # op not whitelisted
    {"op": "attr", "bag": "environment", "key": "x"},  # only subject/resource
    {"op": "attr", "bag": "subject"},            # missing key
    {"op": "lit"},                               # missing value
    {"op": "and", "args": []},                   # empty args
    {"op": "not", "args": [{"op": "lit", "value": True},
                           {"op": "lit", "value": False}]},
    {"op": "eq", "left": {"op": "lit", "value": 1}},  # missing right
    {"op": "lt", "left": {"op": "lit", "value": 1},
     "right": {"op": "lit", "value": 2}, "extra": 3},  # unknown field
    "not-an-object",
])
def test_invalid_nodes_rejected(node):
    with pytest.raises(PolicyValidationError):
        validate_node(node)


def test_literal_rejects_non_json_types():
    with pytest.raises(PolicyValidationError):
        validate_node({"op": "lit", "value": {"a": 1}})
