"""Cross-engine consistency properties over all synthetic fixtures.

For every fixture the independently enumerated oracle decides solvability and
the optimum; each search engine must agree on status, every FOUND plan must be
accepted by the independent executor, and the two optimal cost engines
(ucs/astar) must return the oracle's optimum. These properties cannot be
spoofed by the planner because the expected answers come from ``tests.oracle``.
"""

from __future__ import annotations

import math

import pytest

from strips_planner.core.executor import PlanExecutor
from strips_planner.core.search import SearchLimits, SearchStatus, run_search
from tests.conftest import build_problem, load_fixture
from tests.oracle import exhaustive_solve

pytestmark = pytest.mark.unit

FIXTURES = ["resource_ops.json", "door_negative_goal.json"]
UNSOLVABLE_FIXTURES = ["resource_ops_unsolvable.json"]
LIMITS = SearchLimits(max_expanded=50_000, max_depth=100, max_seconds=20.0)
ENGINES = [("bfs", "zero"), ("ucs", "zero"), ("astar", "hmax"),
           ("greedy", "goalcount")]


@pytest.mark.parametrize("fixture_name", FIXTURES)
@pytest.mark.parametrize("algorithm,heuristic", ENGINES)
def test_engine_status_and_execution_agree_with_oracle(fixture_name, algorithm, heuristic) -> None:
    problem = build_problem(load_fixture(fixture_name))
    oracle = exhaustive_solve(problem)
    outcome = run_search(problem, algorithm=algorithm, heuristic=heuristic, limits=LIMITS)

    assert outcome.status == SearchStatus.FOUND
    trace = PlanExecutor(problem).execute(list(outcome.plan))
    assert trace.valid, trace.message
    assert trace.total_cost >= oracle.min_cost - 1e-9
    assert len(outcome.plan) >= oracle.min_length

    if algorithm in ("bfs",):
        assert outcome.optimal is True
        assert len(outcome.plan) == oracle.min_length
    if algorithm in ("ucs", "astar"):
        assert outcome.optimal is True
        assert outcome.path_cost == pytest.approx(oracle.min_cost)
    if algorithm == "greedy":
        assert outcome.optimal is False


@pytest.mark.parametrize("fixture_name", UNSOLVABLE_FIXTURES)
@pytest.mark.parametrize("algorithm,heuristic", ENGINES)
def test_unsolvable_is_reported_by_every_engine(fixture_name, algorithm, heuristic) -> None:
    problem = build_problem(load_fixture(fixture_name))
    oracle = exhaustive_solve(problem)
    assert oracle.solvable is False
    outcome = run_search(problem, algorithm=algorithm, heuristic=heuristic, limits=LIMITS)
    assert outcome.status == SearchStatus.UNSOLVABLE
    assert outcome.plan == ()
    assert outcome.path_cost == math.inf


def test_search_event_log_captures_replayable_intermediate_states(resource_problem) -> None:
    outcome = run_search(
        resource_problem, algorithm="astar", heuristic="hmax",
        limits=LIMITS, capture_events=True, max_events=50,
    )
    assert outcome.status == SearchStatus.FOUND
    assert outcome.event_log
    first = outcome.event_log[0]
    assert {"seq", "event", "depth", "g", "f", "state"} <= set(first)
    assert "(at r1 depot)" in first["state"]
    # Expansion sequence is strictly numbered for replay.
    seqs = [event["seq"] for event in outcome.event_log]
    assert seqs == sorted(seqs)
