"""Budget semantics: expiry must report feasibility/optimality honestly.

The requirement under test: when the search budget expires, the service
must still return the feasible plan found so far while explicitly
flagging that optimality is unproven -- never collapse the unknown into
either a guaranteed-optimal answer or a failure.
"""
from __future__ import annotations

import pytest

from app.planner.replay import replay
from app.planner.solver import SolverConfig, solve
from app.rules.models import SearchStatus


@pytest.mark.budget
def test_tiny_budget_before_any_plan_reports_unknown_feasibility(workshop_problem) -> None:
    result = solve(workshop_problem, SolverConfig(budget_nodes=3, max_steps=8))

    assert result.status == SearchStatus.NO_PLAN_WITHIN_BUDGET
    assert result.plan is None
    assert not result.optimal
    assert result.nodes_expanded > result.budget_nodes or result.nodes_expanded == 4
    assert "unproven" in result.reason or "unproven" in result.reason.lower()


@pytest.mark.budget
def test_budget_expiry_during_proof_returns_feasible_plan_unproven(workshop_problem) -> None:
    # 800 nodes is enough to discover the makespan-4 plan but not enough
    # to exhaustively close bound 3 (verified independently by the
    # 3000-node OPTIMAL run in the same fixture).
    result = solve(workshop_problem, SolverConfig(budget_nodes=800, max_steps=8))

    assert result.status == SearchStatus.FEASIBLE_UNPROVEN
    assert not result.optimal
    assert result.plan is not None
    assert result.makespan == 4
    # The returned feasible plan must still be independently valid.
    assert replay(workshop_problem, result.plan).is_valid
    # Reason names the unfinished bound and the bounds already proven.
    assert "bound 3" in result.reason
    assert "[0, 1, 2]" in result.reason


@pytest.mark.budget
def test_larger_budget_completes_the_optimality_proof(workshop_problem) -> None:
    result = solve(workshop_problem, SolverConfig(budget_nodes=5000, max_steps=8))
    assert result.status == SearchStatus.OPTIMAL
    assert result.optimal
    assert result.makespan == 4


@pytest.mark.budget
def test_budget_monotonicity_larger_budget_never_loses_information(workshop_problem) -> None:
    # A bigger budget cannot turn FEASIBLE_UNPROVEN into a false INFEASIBLE;
    # it either stays unproven or gets promoted to OPTIMAL with the same plan.
    medium = solve(workshop_problem, SolverConfig(budget_nodes=800, max_steps=8))
    large = solve(workshop_problem, SolverConfig(budget_nodes=50000, max_steps=8))

    assert medium.status == SearchStatus.FEASIBLE_UNPROVEN
    assert large.status == SearchStatus.OPTIMAL
    assert large.makespan == medium.makespan == 4
    assert replay(workshop_problem, large.plan).is_valid
