"""Unit tests for the rule language and domain parser."""

from __future__ import annotations

from fractions import Fraction

import pytest

from tplan.conditions import condition_from_dict
from tplan.model import Problem


def test_comparison_uses_exact_rationals() -> None:
    cond = condition_from_dict(
        {"fluent": {"id": "x", "op": "==", "value": "1/3"}}, {"x"}
    )
    assert cond.evaluate({"x": Fraction(1, 3)})
    assert not cond.evaluate({"x": Fraction(333333333, 1000000000)})


def test_nested_boolean_combinators() -> None:
    cond = condition_from_dict(
        {"all": [
            {"fluent": {"id": "x", "op": ">=", "value": 0}},
            {"any": [
                {"fluent": {"id": "y", "op": "==", "value": 2}},
                {"not": {"fluent": {"id": "z", "op": "==", "value": 1}}},
            ]},
        ]},
        {"x", "y", "z"},
    )
    assert cond.evaluate({"x": 1, "y": 0, "z": 0})
    assert not cond.evaluate({"x": 1, "y": 0, "z": 1})
    assert cond.evaluate({"x": 0, "y": 2, "z": 1})


def test_empty_all_is_true_empty_any_is_false() -> None:
    assert condition_from_dict({"all": []}, set()).evaluate({})
    assert not condition_from_dict({"any": []}, set()).evaluate({})


def test_unknown_fluent_rejected() -> None:
    with pytest.raises(ValueError, match="undeclared fluent"):
        condition_from_dict({"fluent": {"id": "ghost", "op": "==", "value": 1}}, {"x"})


def test_unknown_op_rejected() -> None:
    with pytest.raises(ValueError, match="unknown comparison op"):
        condition_from_dict({"fluent": {"id": "x", "op": "~", "value": 1}}, {"x"})


def test_parser_rejects_duplicate_actions_and_bad_duration() -> None:
    with pytest.raises(ValueError, match="duplicate action id"):
        Problem.from_dict({
            "horizon": 2, "fluents": {"x": 0}, "resources": [],
            "actions": [
                {"id": "a", "duration": 1, "effects": []},
                {"id": "a", "duration": 1, "effects": []},
            ],
            "goal": {"all": []},
        })
    with pytest.raises(ValueError, match="duration must be a non-negative integer"):
        Problem.from_dict({
            "horizon": 2, "fluents": {"x": 0}, "resources": [],
            "actions": [{"id": "a", "duration": -1, "effects": []}],
            "goal": {"all": []},
        })


def test_parser_rejects_unknown_resource_reference() -> None:
    with pytest.raises(ValueError, match="undeclared resource"):
        Problem.from_dict({
            "horizon": 2, "fluents": {"x": 0},
            "resources": [{"id": "r", "capacity": 1}],
            "actions": [{
                "id": "a", "duration": 1, "effects": [],
                "resource_use": [{"resource": "ghost", "amount": 1}],
            }],
            "goal": {"all": []},
        })


def test_parser_rejects_action_request_above_capacity() -> None:
    with pytest.raises(ValueError, match="capacity"):
        Problem.from_dict({
            "horizon": 2, "fluents": {"x": 0},
            "resources": [{"id": "r", "capacity": 1}],
            "actions": [{
                "id": "a", "duration": 1, "effects": [],
                "resource_use": [{"resource": "r", "amount": 2}],
            }],
            "goal": {"all": []},
        })


def test_parser_rejects_effect_on_undeclared_fluent() -> None:
    with pytest.raises(ValueError, match="undeclared fluent"):
        Problem.from_dict({
            "horizon": 2, "fluents": {"x": 0}, "resources": [],
            "actions": [{"id": "a", "duration": 1,
                         "effects": [{"fluent": "ghost", "op": "+", "amount": 1}]}],
            "goal": {"all": []},
        })


def test_parser_rejects_negative_horizon() -> None:
    with pytest.raises(ValueError, match="horizon"):
        Problem.from_dict({"horizon": -1, "fluents": {}, "actions": [], "goal": {}})
