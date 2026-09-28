"""Independent executor: plans are replayed step by step from the init state."""

from __future__ import annotations

import pytest

from strips_planner.executor import execute_plan


def _signatures(plan):
    # Canonical GroundAction.signature rendering: ", " between arguments.
    return [f"{a['name']}({', '.join(a['args'])})" for a in plan]


def test_valid_plan_is_replayed_and_cost_summed(grounded):
    gp = grounded("problem_solvable.json")
    plan = [
        {"name": "move", "args": ["w1", "bench", "press"]},
        {"name": "press", "args": ["w1"]},
        {"name": "move", "args": ["w1", "press", "oven"]},
        {"name": "heat", "args": ["w1"]},
        {"name": "move", "args": ["w1", "oven", "dock"]},
        {"name": "ship", "args": ["w1"]},
    ]
    report = execute_plan(gp, plan)
    assert report.valid is True
    assert report.goal_reached is True
    assert report.total_cost == 8
    assert [s.action for s in report.steps] == _signatures(plan)
    # Each step records concrete predecessor and successor states.
    first = report.steps[0]
    assert "at(w1, bench)" in [a for a in _texts(first.state_before)]
    assert "at(w1, press)" in [a for a in _texts(first.state_after)]


def test_step_with_failed_precondition_reports_exact_step(grounded):
    gp = grounded("problem_solvable.json")
    plan = [
        # w1 starts at the bench, not the oven: precondition fails at step 0.
        {"name": "move", "args": ["w1", "oven", "dock"]},
    ]
    report = execute_plan(gp, plan)
    assert report.valid is False
    assert report.failure["category"] == "state_conflict"
    assert report.failure["code"] == "PRECONDITION_FAILED"
    assert report.failure["step"] == 0
    assert report.failure["details"][0]["missing_positive"] == ["at(w1, oven)"]


def test_plan_ending_before_goal_is_invalid(grounded):
    gp = grounded("problem_solvable.json")
    plan = [{"name": "move", "args": ["w1", "bench", "press"]}]
    report = execute_plan(gp, plan)
    assert report.valid is False
    assert report.failure["code"] == "GOAL_NOT_REACHED"
    assert "shipped(w1)" in report.failure["missing_positive"]


def test_unknown_action_is_input_error_with_step(grounded):
    gp = grounded("problem_solvable.json")
    report = execute_plan(gp, [{"name": "teleport", "args": ["w1"]}])
    assert report.valid is False
    assert report.failure["category"] == "input_error"
    assert report.failure["code"] == "UNKNOWN_ACTION"


def test_wrong_arity_is_input_error(grounded):
    gp = grounded("problem_solvable.json")
    report = execute_plan(gp, [{"name": "press", "args": []}])
    assert report.valid is False
    assert report.failure["code"] == "ACTION_ARITY_MISMATCH"
    assert report.failure["step"] == 0


def test_second_half_of_plan_still_checked_independently(grounded):
    # A valid prefix followed by an inapplicable step must fail at step 2
    # and keep the prefix as evidence.
    gp = grounded("problem_solvable.json")
    plan = [
        {"name": "move", "args": ["w1", "bench", "press"]},
        {"name": "press", "args": ["w1"]},
        # press is no longer clear-source issue: moving back to bench is
        # allowed, but pressing again fails because pressed(w1) holds.
        {"name": "press", "args": ["w1"]},
    ]
    report = execute_plan(gp, plan)
    assert report.valid is False
    assert report.failure["step"] == 2
    assert len(report.steps) == 2
    assert report.failure["category"] == "state_conflict"


def _texts(state):
    from strips_planner.model import atom_text

    return [atom_text(a) for a in sorted(state)]
