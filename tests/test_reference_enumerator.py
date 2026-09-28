"""Concrete tests for the small-grid exhaustive reference enumerator."""
from __future__ import annotations

import pytest

from app.planner.enumerate import exhaustive_reference, grounded_placements
from app.planner.replay import replay
from app.rules.models import (
    Action,
    Plan,
    Problem,
    ScheduledAction,
)


@pytest.mark.reference
def test_grounded_placements_respect_makespan_and_duration_ranges() -> None:
    problem = Problem(
        name="ground",
        horizon=3,
        goal={"fact": {"fact": "done", "op": "==", "value": 1}},
        actions=[Action(name="work", duration_min=1, duration_max=2,
                        effects=[{"fact": "done", "op": "=", "value": 1}])],
    )
    placements = {(p.start, p.duration) for p in grounded_placements(problem, makespan_limit=2)}
    # duration 1 starts 0..1; duration 2 starts 0 only; all end <= 2.
    assert placements == {(0, 1), (1, 1), (0, 2)}


@pytest.mark.reference
def test_reference_finds_empty_plan_when_goal_holds_initially() -> None:
    problem = Problem(
        name="trivial",
        horizon=2,
        initial={"ready": 1},
        goal={"fact": {"fact": "ready", "op": "==", "value": 1}},
        actions=[Action(name="work", duration_min=1,
                        effects=[{"fact": "ready", "op": "=", "value": 0}])],
    )
    answer = exhaustive_reference(problem, max_steps=4, max_occurrences_per_action=2)
    assert answer.found
    assert answer.optimal_makespan == 0
    assert answer.plan is not None and answer.plan.steps == []


@pytest.mark.reference
def test_reference_proves_infeasible_when_goal_unreachable() -> None:
    problem = Problem(
        name="impossible",
        horizon=3,
        initial={"x": 0},
        goal={"fact": {"fact": "x", "op": "==", "value": 5}},
        actions=[Action(name="inc", duration_min=1,
                        effects=[{"fact": "x", "op": "+=", "value": 1}])],
    )
    # Only 3 time slots but each action instance needs its own non-overlap?
    # No resource -> multiple at same time are allowed, but write/write
    # clashes on x forbid same-end stacking; optimal reachable is <= 3.
    answer = exhaustive_reference(problem, max_steps=6, max_occurrences_per_action=3)
    assert not answer.found
    assert answer.optimal_makespan is None
    assert answer.schedules_evaluated > 1


@pytest.mark.reference
def test_reference_optimal_makespan_for_drone_is_concrete_value(drone_problem) -> None:
    answer = exhaustive_reference(
        drone_problem, max_steps=5, max_occurrences_per_action=4
    )
    assert answer.found
    # fly is duration 2 on an exclusive resource: two deliveries need two
    # back-to-back flights [0,2) and [2,4); recharge pulses between.
    assert answer.optimal_makespan == 4
    result = replay(drone_problem, answer.plan)
    assert result.is_valid
    assert result.final_state["delivered"] == 2
    starts_durations = sorted((s.start, s.duration) for s in answer.plan.steps
                              if s.action == "fly")
    assert starts_durations == [(0, 2), (2, 2)]  # boundary release in use


@pytest.mark.reference
def test_reference_workshop_optimum_uses_boundary_release(workshop_problem) -> None:
    answer = exhaustive_reference(
        workshop_problem, max_steps=6, max_occurrences_per_action=3
    )
    assert answer.found
    assert answer.optimal_makespan == 4
    result = replay(workshop_problem, answer.plan)
    assert result.is_valid
    assert result.final_state["polished"] >= 1
    assert result.final_state["pieces"] >= 2


@pytest.mark.reference
def test_reference_evaluates_a_large_positive_number_of_schedules(workshop_problem) -> None:
    answer = exhaustive_reference(
        workshop_problem, max_steps=5, max_occurrences_per_action=3
    )
    assert answer.schedules_evaluated > 100  # genuinely exhaustive, not a stub
