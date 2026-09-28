"""Tests for negative goals, empty-effect self loops and add-wins aliasing."""

from __future__ import annotations

import pytest

from strips_planner.core.executor import PlanExecutor
from strips_planner.core.search import SearchLimits, SearchStatus, run_search
from strips_planner.core.state import apply_action
from tests.conftest import build_problem, load_fixture
from tests.oracle import exhaustive_solve

pytestmark = pytest.mark.unit

LIMITS = SearchLimits(max_expanded=5_000, max_depth=20, max_seconds=10.0)


@pytest.fixture
def door_problem():
    return build_problem(load_fixture("door_negative_goal.json"))


def test_negative_goal_needs_delete_effect(door_problem) -> None:
    # Goal is purely negative: (locked) must be absent. init has it present,
    # so a delete-effect action is mandatory.
    oracle = exhaustive_solve(door_problem)
    assert oracle.solvable
    assert oracle.min_length == 1
    assert oracle.cost_plan == ("(unlock_door)",)
    assert oracle.min_cost == 2.0

    for algorithm, heuristic in (("bfs", "zero"), ("ucs", "zero"),
                                 ("astar", "hmax"), ("greedy", "goalcount")):
        outcome = run_search(door_problem, algorithm=algorithm,
                             heuristic=heuristic, limits=LIMITS)
        assert outcome.status == SearchStatus.FOUND
        trace = PlanExecutor(door_problem).execute(list(outcome.plan))
        assert trace.valid, trace.message
        assert trace.final_state and "(locked)" not in trace.final_state


def test_unlock_lock_cycle_dedupes_and_terminates(door_problem) -> None:
    # Repeated unlock/lock toggles one fluent; search must terminate and never
    # expand a state twice despite arbitrarily long cyclic action strings.
    outcome = run_search(door_problem, algorithm="bfs", limits=LIMITS)
    assert outcome.status == SearchStatus.FOUND
    oracle = exhaustive_solve(door_problem)
    assert outcome.expanded <= oracle.reachable
    assert outcome.generated >= outcome.expanded


def test_empty_effect_action_is_self_loop_and_still_grounded(door_problem) -> None:
    # wait(r1) has neither add nor delete: it applies as the identity. It must
    # remain applicable (not be pruned at grounding) but never create a new
    # state in search.
    wait = next(a for a in door_problem.ground_actions if a.label == "(wait r1)")
    init = frozenset(door_problem.init)
    assert apply_action(wait, init) == init
    outcome = run_search(door_problem, algorithm="bfs", limits=LIMITS)
    # The returned optimum never wastes a step on a self loop.
    assert "(wait r1)" not in outcome.plan


def test_parameter_aliasing_add_delete_overlap_uses_fixed_add_wins(door_problem) -> None:
    # (patrol r1 dock dock) deletes and adds (at r1 dock) via ?from == ?to.
    # The fixed rule is delete-first/add-wins: the fluent survives and the
    # action stays executable (door unlocked first).
    patrol = next(a for a in door_problem.ground_actions
                  if a.label == "(patrol r1 dock dock)")
    init = frozenset(door_problem.init)
    unlock = next(a for a in door_problem.ground_actions if a.label == "(unlock_door)")
    unlocked = apply_action(unlock, init)
    after = apply_action(patrol, unlocked)
    assert "(at r1 dock)" in {str(a) for a in after}

    trace = PlanExecutor(door_problem).execute(
        ["(unlock_door)", "(patrol r1 dock dock)"], enforce_goal=False
    )
    assert trace.valid
    assert "(at r1 dock)" in trace.final_state
    assert "(locked)" not in trace.final_state
