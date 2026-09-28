"""Concrete assertions for the condition/effect mini language."""
from __future__ import annotations

import pytest

from app.rules.conditions import (
    apply_effects,
    effect_facts,
    evaluate,
    referenced_facts,
    validate_condition,
    validate_effects,
)
from app.rules.errors import ValidationFailure


@pytest.mark.semantics
def test_closed_world_default_reads_unset_fact_as_zero() -> None:
    assert evaluate({"fact": {"fact": "x", "op": "==", "value": 0}}, {})
    assert evaluate({"fact": {"fact": "x", "op": "<", "value": 1}}, {})


@pytest.mark.semantics
def test_boolean_and_nested_conditions_evaluate_concretely() -> None:
    state = {"a": 2, "b": 0, "flag": True}
    assert evaluate(
        {"all": [
            {"fact": {"fact": "a", "op": ">=", "value": 2}},
            {"any": [
                {"fact": {"fact": "b", "op": "==", "value": 1}},
                {"fact": {"fact": "flag", "op": "==", "value": True}},
            ]},
            {"not": {"fact": {"fact": "a", "op": "==", "value": 9}}},
        ]},
        state,
    )


@pytest.mark.semantics
def test_effects_return_new_state_and_never_mutate_input() -> None:
    original = {"energy": 3, "done": False}
    snapshot = dict(original)
    updated = apply_effects(
        [
            {"fact": "energy", "op": "-=", "value": 2},
            {"fact": "done", "op": "=", "value": True},
        ],
        original,
    )
    assert updated == {"energy": 1, "done": True}
    assert original == snapshot


@pytest.mark.semantics
def test_arithmetic_on_boolean_is_rejected_not_coerced() -> None:
    with pytest.raises(ValidationFailure):
        apply_effects([{"fact": "done", "op": "+=", "value": 1}], {"done": True})


@pytest.mark.semantics
def test_order_comparison_between_bool_and_int_is_rejected() -> None:
    with pytest.raises(ValidationFailure):
        evaluate({"fact": {"fact": "flag", "op": ">=", "value": 1}}, {"flag": True})


@pytest.mark.semantics
def test_malformed_condition_is_an_error_not_a_false_result() -> None:
    with pytest.raises(ValidationFailure):
        evaluate({"factx": 1}, {})
    with pytest.raises(ValidationFailure):
        validate_condition({"all": {"fact": {"fact": "a", "op": "==", "value": 1}}})
    with pytest.raises(ValidationFailure):
        validate_condition({"fact": {"fact": "a", "op": "~", "value": 1}})
    with pytest.raises(ValidationFailure):
        validate_condition({"fact": {"fact": "a", "op": "==", "value": 1.5}})


@pytest.mark.semantics
def test_fact_extraction_feeds_conflict_analysis() -> None:
    condition = {"all": [{"fact": {"fact": "x", "op": "==", "value": 1}},
                         {"fact": {"fact": "y", "op": "==", "value": 1}}]}
    assert referenced_facts(condition) == {"x", "y"}
    assert effect_facts([{"fact": "z", "op": "=", "value": 1}]) == {"z"}


@pytest.mark.semantics
def test_validate_effects_rejects_bad_shapes() -> None:
    with pytest.raises(ValidationFailure):
        validate_effects([{"fact": "x"}])  # missing value
    with pytest.raises(ValidationFailure):
        validate_effects([{"fact": "x", "op": "??", "value": 1}])
