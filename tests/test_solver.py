"""Concrete tests for the budgeted chronological DFS solver."""
from __future__ import annotations

import pytest

from app.planner.replay import replay
from app.planner.solver import SolverConfig, solve
from app.rules.models import (
    Action,
    Plan,
    Problem,
    SearchStatus,
)


@pytest.mark.solver
def test_solver_finds_optimal_drone_plan_and_it_replays_valid(drone_problem) -> None:
    result = solve(drone_problem, SolverConfig(budget_nodes=100_000, max_steps=8))

    assert result.status == SearchStatus.OPTIMAL
    assert result.optimal
    assert result.makespan == 4
    assert result.plan is not None
    replay_result = replay(drone_problem, result.plan)
    assert replay_result.is_valid
    assert replay_result.final_state["delivered"] == 2
    # Independent gate result reflected in search metadata.
    assert result.nodes_expanded > 0
    assert result.budget_nodes == 100_000


@pytest.mark.solver
def test_solver_reactor_picks_single_pump_not_the_invalid_combo(reactor_problem) -> None:
    result = solve(reactor_problem, SolverConfig(budget_nodes=100_000, max_steps=6))
    assert result.status == SearchStatus.OPTIMAL
    assert result.makespan == 3
    actions = [(s.action, s.start, s.duration) for s in result.plan.steps]
    assert actions == [("run_pump", 0, 3)]


@pytest.mark.solver
def test_solver_reports_infeasible_without_faking_success() -> None:
    problem = Problem(
        name="infeasible",
        horizon=2,
        initial={"x": 0},
        goal={"fact": {"fact": "x", "op": "==", "value": 9}},
        actions=[Action(name="inc", duration_min=1,
                        effects=[{"fact": "x", "op": "+=", "value": 1}])],
    )
    result = solve(problem, SolverConfig(budget_nodes=100_000, max_steps=6))
    assert result.status == SearchStatus.INFEASIBLE
    assert result.plan is None
    assert result.makespan is None
    assert not result.optimal


@pytest.mark.solver
def test_goal_already_true_yields_zero_makespan_optimal_empty_plan() -> None:
    problem = Problem(
        name="done",
        horizon=2,
        initial={"ready": 1},
        goal={"fact": {"fact": "ready", "op": "==", "value": 1}},
        actions=[Action(name="work", duration_min=1,
                        effects=[{"fact": "ready", "op": "=", "value": 0}])],
    )
    result = solve(problem)
    assert result.status == SearchStatus.OPTIMAL
    assert result.makespan == 0
    assert result.plan == Plan(steps=[])


@pytest.mark.solver
def test_solver_rejects_precondition_only_paths_and_finds_real_solution() -> None:
    # Need key==1 before unlock; unlock lasts (invariant) and opens door.
    problem = Problem(
        name="gate",
        horizon=4,
        initial={"key": 0, "open": 0},
        goal={"fact": {"fact": "open", "op": "==", "value": 1}},
        actions=[
            Action(name="make_key", duration_min=1,
                   effects=[{"fact": "key", "op": "=", "value": 1}]),
            Action(name="unlock", duration_min=2,
                   precondition={"fact": {"fact": "key", "op": "==", "value": 1}},
                   invariant={"fact": {"fact": "key", "op": "==", "value": 1}},
                   effects=[{"fact": "open", "op": "=", "value": 1}]),
        ],
    )
    result = solve(problem, SolverConfig(budget_nodes=100_000, max_steps=4))
    assert result.status == SearchStatus.OPTIMAL
    # make_key [0,1), unlock can start at 1 -> ends at 3, makespan 3.
    assert result.makespan == 3
    assert replay(problem, result.plan).is_valid
