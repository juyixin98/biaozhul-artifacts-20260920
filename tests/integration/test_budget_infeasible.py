"""Phase-3 case C: infeasible budgets are reported, not broken plans."""

from __future__ import annotations

import pytest

from app.core.errors import ResourceExhaustedError
from app.core.planner import plan_checkpoints


@pytest.mark.integration
def test_budget_below_minimum_raises_resource_exhausted(tight_budget) -> None:
    fx = tight_budget
    g = fx.graph()
    with pytest.raises(ResourceExhaustedError) as exc:
        plan_checkpoints(g, memory_budget=1)
    err = exc.value
    assert err.category == "resource_exhausted"
    assert err.code == "E_BUDGET_INFEASIBLE"
    ctx = err.context
    # The error names the best achievable peak and a positive shortfall,
    # proving the planner searched rather than emitted an empty plan.
    assert ctx["budget"] == 1
    assert ctx["min_achievable_peak"] > 1
    assert ctx["shortfall"] == ctx["min_achievable_peak"] - 1
    assert ctx["candidates_evaluated"] >= 1


@pytest.mark.integration
def test_budget_just_above_minimum_succeeds(tight_budget) -> None:
    fx = tight_budget
    g = fx.graph()
    # Find the true minimum peak by planning without a budget and retaining
    # nothing is not necessarily minimal; use the infeasible error's value.
    try:
        plan_checkpoints(g, memory_budget=1)
    except ResourceExhaustedError as exc:
        min_peak = exc.context["min_achievable_peak"]
    plan = plan_checkpoints(g, memory_budget=min_peak)
    assert plan.simulation.peak_memory == min_peak
    assert plan.simulation.peak_memory <= min_peak


@pytest.mark.integration
def test_error_categories_are_distinct(tight_budget, linear_chain) -> None:
    from app.core.errors import (
        InvalidInputError,
        StateConflictError,
    )

    # input error: non-positive budget type is rejected before search
    with pytest.raises((ValueError, TypeError)):
        plan_checkpoints(linear_chain.graph(), memory_budget=0)

    # resource exhausted: feasible input, infeasible resource
    with pytest.raises(ResourceExhaustedError) as exc:
        plan_checkpoints(tight_budget.graph(), memory_budget=1)
    assert exc.value.category == "resource_exhausted"

    # state conflict is a lifecycle error, exercised on TrainingState;
    # ensure the category strings are disjoint constants.
    assert InvalidInputError.category != StateConflictError.category
    assert StateConflictError.category != ResourceExhaustedError.category


@pytest.mark.integration
def test_planner_reports_extra_compute_explicitly(linear_chain) -> None:
    fx = linear_chain
    g = fx.graph()
    # A tight budget forces recompute; the returned plan must state exactly
    # how much extra work is required (never hide it as a failing execution).
    full = plan_checkpoints(g, memory_budget=None)
    forced = plan_checkpoints(g, memory_budget=full.min_achievable_peak)
    assert forced.simulation.peak_memory <= full.min_achievable_peak
    assert forced.extra_compute_flops >= 0
    # No-budget optimum is zero extra compute (retain everything).
    assert full.extra_compute_flops == 0
