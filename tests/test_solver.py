"""Solver tests: optimality, proven unsat, budget semantics, replay checks."""

from __future__ import annotations

import pytest

from tplan.model import FailureCode, Problem
from tplan.solver import Budget, Solver
from tplan.simulator import simulate

from .cases import (
    BOUNDARY_RELEASE,
    FRACTIONAL,
    MIDPOINT_VIOLATION,
    OPTIMALITY_GAP,
    UNSAT,
    ZERO_DURATION,
)


def test_solver_finds_proven_optimal_plan_and_replay_validates() -> None:
    problem = Problem.from_dict(BOUNDARY_RELEASE)
    result = Solver(problem).solve()
    assert result.status == "optimal"
    assert result.failure_code is None
    assert result.best_cost == 2
    assert result.goal_time == 5
    # Concrete plan: carry A then B across the half-open boundary at t=3.
    assert [(s.action_id, s.start, s.end) for s in result.schedule] == [
        ("A", 0, 3),
        ("B", 3, 5),
    ]
    # The plan the solver returns must survive the independent replay and
    # actually reach the goal -- no self-certification.
    outcome = simulate(problem, list(result.schedule))
    assert outcome.ok and outcome.goal_met


def test_solver_proves_unsat_by_exhaustion() -> None:
    result = Solver(Problem.from_dict(UNSAT)).solve()
    assert result.status == "unsat"
    assert result.failure_code is FailureCode.UNSAT_PROVEN
    assert result.schedule == ()
    assert f"horizon {3}" in result.message


def test_zero_duration_plan_completes_at_its_instant() -> None:
    result = Solver(Problem.from_dict(ZERO_DURATION)).solve()
    assert result.status == "optimal"
    assert result.best_cost == 1
    # The action has zero duration and is startable at t=0; completion is t=0.
    assert result.goal_time == 0
    assert [(x.action_id, x.start, x.end) for x in result.schedule] == [("Z", 0, 0)]


def test_exact_fractional_accumulation_reaches_goal() -> None:
    result = Solver(Problem.from_dict(FRACTIONAL), Budget(node_limit=100_000)).solve()
    assert result.status == "optimal"
    assert result.best_cost == 3
    assert result.goal_time == 3


def test_solver_avoids_plan_whose_midpoint_invariant_fails() -> None:
    """Any schedule of F with G ending inside F's window is invalid.

    The goal is vacuously true, so the optimal feasible completion is the
    empty plan (cost 0); starting F alone [0,4) is also feasible, but the
    solver must never return F+G (the interior-failing combination).
    """
    problem = Problem.from_dict(MIDPOINT_VIOLATION)
    result = Solver(problem).solve()
    assert result.status == "optimal"
    for s in result.schedule:
        if s.action_id == "G":
            f = next((x for x in result.schedule if x.action_id == "F"), None)
            assert f is None or not (f.start < s.end <= f.end)


def test_budget_exhausted_reports_feasible_but_unproven() -> None:
    """There exists a budget that stops search after the longer 3-action plan
    is found but before the 1-action plan is proven minimal. We scan budgets
    rather than hard-coding a node count (robust to ordering tweaks)."""
    problem = Problem.from_dict(OPTIMALITY_GAP)

    full = Solver(problem).solve()
    assert full.status == "optimal"
    assert full.best_cost == 1  # one "big" jump

    statuses: dict[str, object] = {}
    bounded = None
    for budget in range(1, full.nodes_expanded + 1):
        r = Solver(problem, Budget(node_limit=budget, deadline_monotonic=None)).solve()
        statuses[r.status] = budget
        if r.status == "feasible_not_proven_optimal" and r.best_cost and r.best_cost > 1:
            bounded = r
            break

    assert bounded is not None, (
        f"expected an intermediate feasible-but-unproven budget, got {statuses}"
    )
    assert bounded.failure_code is FailureCode.BUDGET_EXHAUSTED
    assert bounded.schedule != ()  # a concrete feasible plan is returned
    outcome = simulate(problem, list(bounded.schedule))
    assert outcome.ok and outcome.goal_met  # still a genuinely valid plan
    assert "optimality" in bounded.message and "proven" in bounded.message
    events = [e.get("event") for e in bounded.trace]
    assert "budget_exhausted" in events
    assert "plan_found" in events


def test_time_budget_zero_stops_immediately_as_budget_exhausted() -> None:
    import time

    problem = Problem.from_dict(UNSAT)
    deadline = time.monotonic()  # already expired
    result = Solver(problem, Budget(node_limit=10**9, deadline_monotonic=deadline)).solve()
    assert result.status == "budget_exhausted"
    assert result.failure_code is FailureCode.BUDGET_EXHAUSTED


def test_search_trace_records_pruning_reasons() -> None:
    # B cannot start before A's end effect makes x>=1; the trace must show
    # the start-condition pruning decision.
    problem = Problem.from_dict(BOUNDARY_RELEASE)
    result = Solver(problem).solve()
    prune_events = [e for e in result.trace if e.get("event", "").startswith("prune_")]
    assert any(e["event"] == "prune_start_condition" for e in prune_events)
    early = next(e for e in prune_events if e["event"] == "prune_start_condition")
    assert early["action"] == "B"
    assert early["t"] < 3


def test_solver_never_returns_goal_while_action_still_running() -> None:
    """A long action sets y=1 only at its END; the goal on y cannot be met
    mid-run even though the end is scheduled."""
    raw = {
        "horizon": 5, "fluents": {"y": 0}, "resources": [],
        "actions": [
            {"id": "long", "duration": 4, "start_condition": [], "invariant": [],
             "effects": [{"fluent": "y", "op": "=", "amount": 1}]},
        ],
        "goal": {"fluent": {"id": "y", "op": ">=", "value": 1}},
    }
    result = Solver(Problem.from_dict(raw)).solve()
    assert result.status == "optimal"
    assert result.goal_time == 4
    assert result.schedule[0].end == 4


@pytest.mark.parametrize("budget", [1, 2, 5, 25, 100])
def test_budget_boundaries_never_corrupt_verdict(budget: int) -> None:
    """At every small budget the returned plan (if any) must replay; status
    must be one of the explicit categories."""
    problem = Problem.from_dict(BOUNDARY_RELEASE)
    result = Solver(problem, Budget(node_limit=budget, deadline_monotonic=None)).solve()
    assert result.status in {
        "optimal",
        "feasible_not_proven_optimal",
        "budget_exhausted",
        "unsat",
    }
    if result.found:
        outcome = simulate(problem, list(result.schedule))
        assert outcome.ok and outcome.goal_met
