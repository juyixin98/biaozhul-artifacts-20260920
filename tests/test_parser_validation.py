"""Parser and static validation: every malformed input maps to a fixed code."""

from __future__ import annotations

import pytest

from strips_planner.errors import (
    ACTION_NAME_DUPLICATE,
    EFFECT_ADD_DELETE_CONFLICT,
    GOAL_CONTRADICTION,
    LITERAL_MALFORMED,
    OBJECT_UNDECLARED,
    PARAM_DUPLICATE,
    PREDICATE_ARITY_CONFLICT,
    PRECONDITION_CONTRADICTION,
    UNBOUND_VARIABLE,
    ValidationError,
)
from strips_planner.parser import parse_domain, parse_problem
from strips_planner.validation import validate


def _domain(actions, name="d"):
    return {"name": name, "actions": actions}


def _problem(goal, objects=("o1", "o2"), init=None):
    return {
        "name": "p",
        "objects": list(objects),
        "init": init or [],
        "goal": goal,
    }


BASE_ACTION = {
    "name": "a",
    "parameters": ["?x", "?y"],
    "preconditions": {"pos": ["p(?x)"], "neg": []},
    "add": ["q(?y)"],
    "delete": ["p(?x)"],
    "cost": 1,
}


def _validate(domain_data, problem_data):
    domain = parse_domain(domain_data)
    problem = parse_problem(problem_data, domain)
    validate(domain, problem)


def test_valid_domain_and_problem_pass():
    _validate(_domain([BASE_ACTION]),
              _problem({"pos": ["q(o2)"], "neg": []}))


def test_missing_domain_name_is_input_error():
    with pytest.raises(ValidationError) as exc:
        parse_domain({"actions": [BASE_ACTION]})
    assert exc.value.category == "input_error"
    assert exc.value.code == "DOMAIN_NAME_MISSING"


def test_duplicate_action_name_reported():
    with pytest.raises(ValidationError) as exc:
        _validate(_domain([BASE_ACTION, dict(BASE_ACTION)]),
                  _problem({"pos": ["q(o2)"], "neg": []}))
    assert exc.value.code == ACTION_NAME_DUPLICATE


def test_duplicate_parameter_reported():
    action = dict(BASE_ACTION, parameters=["?x", "?x"])
    with pytest.raises(ValidationError) as exc:
        _validate(_domain([action]), _problem({"pos": ["q(o2)"], "neg": []}))
    assert exc.value.code == PARAM_DUPLICATE


def test_unbound_variable_reported():
    action = dict(BASE_ACTION, add=["q(?z)"])
    with pytest.raises(ValidationError) as exc:
        _validate(_domain([action]), _problem({"pos": ["q(o2)"], "neg": []}))
    assert exc.value.code == UNBOUND_VARIABLE


def test_arity_conflict_across_actions_reported():
    other = dict(BASE_ACTION, name="b", preconditions={"pos": ["p(?x,?y)"], "neg": []})
    with pytest.raises(ValidationError) as exc:
        _validate(_domain([BASE_ACTION, other]),
                  _problem({"pos": ["q(o2)"], "neg": []}))
    assert exc.value.code == PREDICATE_ARITY_CONFLICT


def test_contradictory_preconditions_rejected():
    action = dict(BASE_ACTION,
                  preconditions={"pos": ["p(?x)"], "neg": ["p(?x)"]})
    with pytest.raises(ValidationError) as exc:
        _validate(_domain([action]), _problem({"pos": ["q(o2)"], "neg": []}))
    assert exc.value.code == PRECONDITION_CONTRADICTION


def test_add_delete_conflict_rejected():
    action = dict(BASE_ACTION, add=["p(?x)"], delete=["p(?x)"])
    with pytest.raises(ValidationError) as exc:
        _validate(_domain([action]), _problem({"pos": ["q(o2)"], "neg": []}))
    assert exc.value.code == EFFECT_ADD_DELETE_CONFLICT


def test_undeclared_object_in_goal_rejected():
    with pytest.raises(ValidationError) as exc:
        _validate(_domain([BASE_ACTION]),
                  _problem({"pos": ["q(ghost)"], "neg": []}))
    assert exc.value.code == OBJECT_UNDECLARED


def test_contradictory_goal_rejected():
    with pytest.raises(ValidationError) as exc:
        _validate(
            _domain([dict(BASE_ACTION, add=["q(?x)", "r(?x)"],
                          delete=["p(?x)"], preconditions={"pos": [], "neg": []})]),
            _problem({"pos": ["q(o1)"], "neg": ["q(o1)"]}),
        )
    assert exc.value.code == GOAL_CONTRADICTION


def test_malformed_literal_rejected():
    action = dict(BASE_ACTION, add=["not a literal ((("])
    with pytest.raises(ValidationError) as exc:
        parse_domain(_domain([action]))
    assert exc.value.code == LITERAL_MALFORMED


def test_non_positive_cost_rejected():
    action = dict(BASE_ACTION, cost=0)
    with pytest.raises(ValidationError) as exc:
        parse_domain(_domain([action]))
    assert exc.value.code == "INVALID_COST"


def test_signed_flat_literal_list_is_parsed():
    action = {
        "name": "a",
        "parameters": ["?x"],
        "preconditions": ["p(?x)", "~q(?x)"],
        "add": ["q(?x)"],
        "delete": ["p(?x)"],
        "cost": 1,
    }
    domain = parse_domain(_domain([action]))
    schema = domain.actions[0]
    assert ("p", "?x") in schema.pre_pos
    assert ("q", "?x") in schema.pre_neg
