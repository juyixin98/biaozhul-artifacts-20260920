"""Tests for the independent executor and STRIPS transition semantics."""

from __future__ import annotations

import pytest

from strips_planner.core.executor import PlanExecutor
from strips_planner.core.state import (
    StateEncoder,
    apply_action,
    applicable,
)
from strips_planner.errors import IssueCode

pytestmark = pytest.mark.unit


def test_apply_uses_delete_then_add_same_predecessor(resource_problem) -> None:
    init = frozenset(resource_problem.init)
    # clear_block site1 is applicable from init (blocked site1 present).
    clear = next(a for a in resource_problem.ground_actions
                 if a.label == "(clear_block site1)")
    state_after = apply_action(clear, init)
    # Both effects were judged against init; (blocked site1) disappears and
    # every other init literal survives untouched.
    assert "(blocked site1)" not in {str(x) for x in state_after}
    assert "(at r1 depot)" in {str(x) for x in state_after}
    assert init != state_after  # returns a new state, never mutates


def test_negative_precondition_blocks_applicability(resource_problem) -> None:
    init = frozenset(resource_problem.init)
    move_into_site1 = next(a for a in resource_problem.ground_actions
                           if a.label == "(move r1 depot site1)")
    # site1 starts blocked: negative precondition (blocked ?to) must fail.
    assert not applicable(move_into_site1, init)
    clear = next(a for a in resource_problem.ground_actions
                 if a.label == "(clear_block site1)")
    unblocked = apply_action(clear, init)
    assert applicable(move_into_site1, unblocked)


def test_state_encoder_dedupes_equal_states(resource_problem) -> None:
    encoder = StateEncoder(resource_problem)
    init = frozenset(resource_problem.init)
    # Round-trip preserves the state.
    assert encoder.decode(encoder.encode(init)) == init
    # A harmless cycle move depot->site1 (once unblocked) and back lands on a
    # state structurally equal to the post-clear state: identical code.
    clear = next(a for a in resource_problem.ground_actions
                 if a.label == "(clear_block site1)")
    s1 = apply_action(clear, init)
    go = next(a for a in resource_problem.ground_actions
              if a.label == "(move r1 depot site1)")
    back = next(a for a in resource_problem.ground_actions
                if a.label == "(move r1 site1 depot)")
    s2 = apply_action(back, apply_action(go, s1))
    # s2 differs from s1 only through move effects; robot returns to depot.
    assert encoder.encode(s2) == encoder.encode(s1)


def test_executor_accepts_valid_step_sequence_but_reports_goal_open(resource_problem) -> None:
    # Clear site1, walk to site2 via site1: every step is applicable, but the
    # crate is never delivered, so the goal remains open.
    plan = [
        "(clear_block site1)",
        "(move r1 depot site1)",
        "(move r1 site1 site2)",
    ]
    strict = PlanExecutor(resource_problem).execute(plan)
    assert not strict.valid
    assert strict.failure_code == IssueCode.GOAL_NOT_REACHED
    assert strict.total_cost == 7.0

    # The same sequence is a valid *execution* when goal enforcement is off.
    relaxed = PlanExecutor(resource_problem).execute(plan, enforce_goal=False)
    assert relaxed.valid, relaxed.message
    assert relaxed.total_cost == 7.0
    assert "(at r1 site2)" in relaxed.final_state


def test_executor_rejects_unknown_action(resource_problem) -> None:
    trace = PlanExecutor(resource_problem).execute(["(teleport r1 mars)"])
    assert not trace.valid
    assert trace.failure_code == IssueCode.UNKNOWN_ACTION
    assert trace.failure_step == 0


def test_executor_reports_missing_positive_precondition(resource_problem) -> None:
    # Moving from site1 without ever being at site1.
    trace = PlanExecutor(resource_problem).execute(["(move r1 site1 site2)"])
    assert not trace.valid
    assert trace.failure_code == IssueCode.MISSING_PRECONDITION
    assert trace.failure_step == 0
    violated = trace.steps[0].violated_literals
    assert any("(at r1 site1)" in lit for lit in violated)


def test_executor_reports_negative_precondition_conflict(resource_problem) -> None:
    # clear_block site1 first succeeds; directly moving into blocked site1 fails.
    trace = PlanExecutor(resource_problem).execute([
        "(move r1 depot site1)",
    ])
    assert not trace.valid
    assert trace.failure_code == IssueCode.NEGATIVE_PRECONDITION_VIOLATED
    assert any("(blocked site1)" in lit
               for lit in trace.steps[0].violated_literals)


def test_executor_detects_goal_not_reached(resource_problem) -> None:
    plan = ["(clear_block site1)"]
    trace = PlanExecutor(resource_problem).execute(plan)
    assert not trace.valid
    assert trace.failure_code == IssueCode.GOAL_NOT_REACHED
    assert trace.failure_step is None
    assert trace.total_cost == 5.0


def test_executor_trace_state_evolution_is_complete(resource_problem) -> None:
    plan = ["(clear_block site1)", "(move r1 depot site1)"]
    trace = PlanExecutor(resource_problem).execute(plan, enforce_goal=False)
    assert trace.valid
    assert len(trace.steps) == 2
    first, second = trace.steps
    assert "(blocked site1)" in first.state_before
    assert "(blocked site1)" not in first.state_after
    assert "(at r1 site1)" in second.state_after
    assert "(at r1 depot)" not in second.state_after
    assert trace.total_cost == 6.0


def test_executor_is_independent_of_search_bookkeeping(resource_problem) -> None:
    # A syntactically grounded label sequence that search would never emit
    # because its first step is inapplicable must still be rejected.
    executor = PlanExecutor(resource_problem)
    trace = executor.execute(["(move r1 site2 site1)", "(move r1 site1 depot)"])
    assert not trace.valid
    assert trace.failure_step == 0
