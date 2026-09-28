"""Tests for greedy search and planner contract details."""

from __future__ import annotations

import pytest

from app.core.errors import ResourceExhaustedError
from app.core.graph import Node, build_graph
from app.core.planner import EXHAUSTIVE_LIMIT, plan_checkpoints
from app.fixtures.graphs import get_fixture


@pytest.mark.unit
@pytest.mark.parametrize("fixture", ["linear_chain", "branching"])
def test_greedy_matches_exhaustive_optimum_on_small_graphs(fixture) -> None:
    fx = get_fixture(fixture)
    g = fx.graph()
    ex = plan_checkpoints(g, None, force_search="exhaustive")
    gr = plan_checkpoints(g, None, force_search="greedy")
    assert gr.search == "greedy"
    assert gr.simulation.peak_memory == ex.simulation.peak_memory
    assert gr.extra_compute_flops == ex.extra_compute_flops


@pytest.mark.unit
def test_large_graph_uses_greedy_by_default_and_satisfies_budget() -> None:
    # Build a deep chain with more than EXHAUSTIVE_LIMIT internal nodes so
    # exhaustive enumeration is never attempted.
    nodes = [Node("x", "input", (), {"shape": [4, 4]})]
    prev = "x"
    n_internal = EXHAUSTIVE_LIMIT + 4
    for i in range(n_internal):
        wid = f"W{i}"
        hid = f"h{i}"
        nodes.append(Node(wid, "parameter", (), {"shape": [4, 4]}))
        nodes.append(Node(hid, "linear", (prev, wid)))
        prev = hid
    nodes.append(Node("loss", "reduce_sum", (prev,)))
    g = build_graph(nodes, ["loss"])

    unconstrained = plan_checkpoints(g, None)
    assert unconstrained.search == "greedy"
    assert unconstrained.extra_compute_flops == 0

    # Force a tight budget: greedy must return a fitting plan that does
    # recompute, or raise a classified resource-exhaustion error.
    min_peak = unconstrained.min_achievable_peak
    if min_peak <= unconstrained.simulation.peak_memory:
        tight = plan_checkpoints(g, memory_budget=min_peak)
        assert tight.simulation.peak_memory <= min_peak
        assert tight.extra_compute_flops >= 0

    with pytest.raises(ResourceExhaustedError) as exc:
        plan_checkpoints(g, memory_budget=1)
    assert exc.value.category == "resource_exhausted"
    assert exc.value.context["min_achievable_peak"] >= min_peak


@pytest.mark.unit
def test_greedy_rejects_unknown_strategy_name(linear_chain) -> None:
    with pytest.raises(ValueError):
        plan_checkpoints(linear_chain.graph(), None, force_search="astar")


@pytest.mark.unit
def test_budget_validation(linear_chain) -> None:
    with pytest.raises(ValueError):
        plan_checkpoints(linear_chain.graph(), memory_budget=0)
    with pytest.raises(TypeError):
        plan_checkpoints(linear_chain.graph(), memory_budget=100.5)
