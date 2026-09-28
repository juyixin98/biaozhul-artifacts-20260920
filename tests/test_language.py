"""Tests for parser + validator: shape errors, semantic issues, grounding."""

from __future__ import annotations

import pytest

from strips_planner.errors import (
    ErrorCategory,
    IssueCode,
    ProblemParseError,
    ProblemValidationError,
)
from strips_planner.language.parser import parse_dict, parse_json
from tests.conftest import build_problem, load_fixture

pytestmark = pytest.mark.unit

MINIMAL_PAYLOAD = {
    "name": "mini",
    "types": ["loc"],
    "objects": {"loc": ["a", "b"]},
    "predicates": {"at": {"types": ["loc"], "static": False},
                   "link": {"types": ["loc", "loc"], "static": True}},
    "actions": [
        {
            "name": "go",
            "cost": 1,
            "parameters": [{"name": "?x", "type": "loc"}, {"name": "?y", "type": "loc"}],
            "preconditions": {"pos": ["(at ?x)", "(link ?x ?y)"], "neg": []},
            "effects": {"add": ["(at ?y)"], "del": ["(at ?x)"]},
        }
    ],
    "init": ["(at a)", "(link a b)"],
    "goal": {"pos": ["(at b)"], "neg": []},
}


def test_fixture_problem_grounds_expected_number_of_actions(resource_problem) -> None:
    # move/express/pick/drop: robot x loc x loc = 9 each, clear: 3 locations,
    # pick/drop additionally vary over 2 crates -> 9 * 2 * 2 + 9 * 2 + 3.
    labels = {a.label for a in resource_problem.ground_actions}
    assert len(labels) == len(resource_problem.ground_actions)  # labels unique
    assert "(move r1 depot site1)" in labels
    assert "(express_move r1 depot site2)" in labels
    assert "(clear_block site1)" in labels
    assert "(pick r1 crate_a depot)" in labels
    assert "(drop r1 crate_b site2)" in labels
    # The blocked site still grounds a move into it; applicability is a runtime
    # concern, grounding must not silently drop it.
    assert "(move r1 depot site1)" in labels


def test_parse_json_rejects_malformed_syntax() -> None:
    with pytest.raises(ProblemParseError) as exc:
        parse_json("{not json")
    assert exc.value.category == ErrorCategory.INPUT_INVALID
    assert exc.value.code == IssueCode.SYNTAX_ERROR


def test_unknown_top_field_is_shape_error() -> None:
    payload = dict(MINIMAL_PAYLOAD)
    payload["bogus"] = 1
    with pytest.raises(ProblemParseError) as exc:
        parse_dict(payload)
    assert exc.value.code == IssueCode.UNKNOWN_FIELD


def test_empty_body_rejected_as_input_error() -> None:
    with pytest.raises(ProblemParseError):
        parse_json("")


@pytest.mark.parametrize(
    "mutate, code",
    [
        (lambda p: p["predicates"].__setitem__("at", {"types": ["missing"]}),
         IssueCode.UNKNOWN_TYPE),
        (lambda p: p["init"].__setitem__(0, "(at nowhere)"),
         IssueCode.UNKNOWN_REFERENCE),
        (lambda p: p["init"].__setitem__(0, "(at a b)"),
         IssueCode.ARITY_MISMATCH),
        (lambda p: p["init"].__setitem__(0, "(link robot1 a)"),
         IssueCode.TYPE_MISMATCH),
    ],
)
def test_semantic_issues_are_validation_errors(mutate, code) -> None:
    payload = {
        **MINIMAL_PAYLOAD,
        "types": ["loc", "robot"],
        "objects": {"loc": ["a", "b"], "robot": ["robot1"]},
        "predicates": {k: dict(v) for k, v in MINIMAL_PAYLOAD["predicates"].items()},
        "init": list(MINIMAL_PAYLOAD["init"]),
    }
    mutate(payload)
    with pytest.raises(ProblemValidationError) as exc:
        build_problem(payload)
    codes = {issue.code for issue in exc.value.issues}
    assert code in codes
    assert exc.value.category == ErrorCategory.INVALID_PROBLEM


def test_add_delete_conflict_is_rejected() -> None:
    payload = {
        "name": "conflict",
        "types": ["loc"],
        "objects": {"loc": ["a"]},
        "predicates": {"p": {"types": ["loc"], "static": False}},
        "actions": [{
            "name": "bad",
            "cost": 1,
            "parameters": [{"name": "?x", "type": "loc"}],
            "preconditions": {"pos": [], "neg": []},
            # Same predecessor state: adding and deleting the same ground atom
            # has no fixed meaning, so it is invalid.
            "effects": {"add": ["(p ?x)"], "del": ["(p ?x)"]},
        }],
        "init": [],
        "goal": {"pos": ["(p a)"], "neg": []},
    }
    with pytest.raises(ProblemValidationError) as exc:
        build_problem(payload)
    assert IssueCode.ADD_DELETE_CONFLICT in {i.code for i in exc.value.issues}


def test_static_predicate_cannot_be_modified() -> None:
    payload = {
        "name": "static-mod",
        "types": ["loc"],
        "objects": {"loc": ["a", "b"]},
        "predicates": {"link": {"types": ["loc", "loc"], "static": True}},
        "actions": [{
            "name": "make-link",
            "cost": 1,
            "parameters": [{"name": "?x", "type": "loc"}, {"name": "?y", "type": "loc"}],
            "preconditions": {"pos": [], "neg": []},
            "effects": {"add": ["(link ?x ?y)"], "del": []},
        }],
        "init": [],
        "goal": {"pos": ["(link a b)"], "neg": []},
    }
    with pytest.raises(ProblemValidationError) as exc:
        build_problem(payload)
    assert IssueCode.STATIC_PREDICATE_MODIFIED in {i.code for i in exc.value.issues}


def test_positive_and_negative_goal_overlap_rejected() -> None:
    payload = {**MINIMAL_PAYLOAD, "goal": {"pos": ["(at b)"], "neg": ["(at b)"]}}
    with pytest.raises(ProblemValidationError) as exc:
        build_problem(payload)
    assert IssueCode.INVALID_GOAL in {i.code for i in exc.value.issues}


def test_unknown_type_in_action_parameter_rejected() -> None:
    payload = {**MINIMAL_PAYLOAD}
    payload["actions"] = [{
        **MINIMAL_PAYLOAD["actions"][0],
        "parameters": [{"name": "?x", "type": "ghost"}],
    }]
    with pytest.raises(ProblemValidationError):
        build_problem(payload)


def test_unused_parameter_is_warning_not_error() -> None:
    payload = {**MINIMAL_PAYLOAD}
    payload["actions"] = [{
        **MINIMAL_PAYLOAD["actions"][0],
        "parameters": [
            {"name": "?x", "type": "loc"},
            {"name": "?y", "type": "loc"},
            {"name": "?z", "type": "loc"},
        ],
    }]
    problem = build_problem(payload)
    assert any(w.code == IssueCode.UNUSED_PARAMETER and "?z" in w.message
               for w in problem.warnings)


def test_cost_must_be_positive_number() -> None:
    payload = {**MINIMAL_PAYLOAD}
    payload["actions"] = [{**MINIMAL_PAYLOAD["actions"][0], "cost": 0}]
    with pytest.raises(ProblemParseError) as exc:
        parse_dict(payload)
    assert exc.value.code == IssueCode.INVALID_COST


def test_list_atom_notation_accepted() -> None:
    payload = {**MINIMAL_PAYLOAD,
               "init": [["at", "a"], ["link", "a", "b"]]}
    problem = build_problem(payload)
    assert any(str(a) == "(at a)" for a in problem.init)


def test_fixture_files_parse() -> None:
    for name in ("resource_ops.json", "resource_ops_unsolvable.json"):
        problem = build_problem(load_fixture(name))
        assert problem.ground_actions
