"""Unit tests for state facts, finite resources, and precondition semantics."""
from __future__ import annotations

import pytest

from htn_planner.models import Condition, Effect, Primitive
from htn_planner.state import ConditionError, State

pytestmark = pytest.mark.unit

def test_fact_and_negation_semantics() -> None:
    state = State(facts=frozenset({("at", "t", "depot")}))
    assert state.satisfies(Condition(kind="fact", name="at", args=["t", "depot"]))
    assert not state.satisfies(Condition(kind="fact", name="at", args=["t", "hub"]))
    assert state.satisfies(Condition(kind="not", name="at", args=["t", "hub"]))


def test_resource_avail_and_bound() -> None:
    state = State(facts=frozenset(), resources={"dock": [1, 0]})
    assert state.satisfies(Condition(kind="avail", name="dock", amount=1))
    state.resources["dock"][1] = 1
    assert not state.satisfies(Condition(kind="avail", name="dock", amount=1))
    assert state.satisfies(Condition(kind="bound", name="dock", amount=1))


def test_condition_on_undeclared_resource_raises() -> None:
    state = State.empty()
    with pytest.raises(ConditionError, match="undeclared resource"):
        state.satisfies(Condition(kind="avail", name="ghost", amount=1))


def test_apply_returns_new_state_and_does_not_mutate() -> None:
    prim = Primitive(
        name="grab",
        precondition=[Condition(kind="avail", name="dock", amount=1)],
        effect=Effect(reserve=[{"resource": "dock", "amount": 1}]),
    )
    before = State(facts=frozenset(), resources={"dock": [1, 0]})
    after = before.apply(prim)
    assert before.resources["dock"][1] == 0
    assert after.resources["dock"][1] == 1
    # Reserve beyond capacity fails executable check even with a happy guard.
    assert after.can_execute(prim)[0] is False


def test_primitive_precondition_failure_is_typed() -> None:
    prim = Primitive(
        name="move",
        precondition=[Condition(kind="fact", name="at", args=["t", "depot"])],
    )
    state = State(facts=frozenset({("at", "t", "elsewhere")}))
    ok, reason = state.can_execute(prim)
    assert ok is False
    assert "absent" in reason
