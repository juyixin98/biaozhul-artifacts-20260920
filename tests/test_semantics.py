"""Transition semantics: single predecessor judgement and fixed conflict rule."""

from __future__ import annotations

import pytest

from strips_planner.errors import PRECONDITION_FAILED, StateConflictError
from strips_planner.model import GroundAction
from strips_planner.semantics import applicable, apply, goal_satisfied

ACTION = GroundAction(
    schema_name="a",
    args=("o1", "o2"),
    pre_pos=frozenset({("p", "o1")}),
    pre_neg=frozenset({("q", "o2")}),
    add_effects=frozenset({("r", "o2")}),
    del_effects=frozenset({("p", "o1")}),
    cost=1,
)


def test_applicable_requires_positive_and_absence_of_negative():
    s = frozenset({("p", "o1")})
    assert applicable(s, ACTION)
    assert not applicable(frozenset(), ACTION)                  # missing positive
    assert not applicable(s | {("q", "o2")}, ACTION)           # negative present


def test_apply_judges_against_one_predecessor_and_replaces_state():
    s = frozenset({("p", "o1"), ("keep",)})
    nxt = apply(s, ACTION)
    assert nxt == frozenset({("r", "o2"), ("keep",)})
    # Original state is untouched (no in-place mutation of the predecessor).
    assert s == frozenset({("p", "o1"), ("keep",)})


def test_apply_raises_state_conflict_with_detailed_reasons():
    with pytest.raises(StateConflictError) as exc:
        apply(frozenset({("q", "o2")}), ACTION)
    assert exc.value.category == "state_conflict"
    assert exc.value.code == PRECONDITION_FAILED
    detail = exc.value.details[0]
    assert detail["missing_positive"] == ["p(o1)"]
    assert detail["present_negative"] == ["q(o2)"]


def test_delete_then_add_rule_is_the_set_union_rule():
    # Even if an action were allowed overlapping effects, the transition
    # formula (s - del) | add computes both from the same predecessor.
    overlapping = GroundAction(
        schema_name="o", args=(),
        pre_pos=frozenset(), pre_neg=frozenset(),
        add_effects=frozenset({("x",)}),
        del_effects=frozenset({("x",)}), cost=1,
    )
    s = frozenset({("x",), ("y",)})
    assert apply(s, overlapping) == frozenset({("y",), ("x",)})


def test_goal_satisfied_checks_both_signs():
    gpos = frozenset({("r",)})
    gneg = frozenset({("q",)})
    assert goal_satisfied(frozenset({("r",)}), gpos, gneg)
    assert not goal_satisfied(frozenset({("r",), ("q",)}), gpos, gneg)
    assert not goal_satisfied(frozenset(), gpos, gneg)
