"""Search kernel tests cross-checked against the independent oracle.

Covers the acceptance scenarios: synthetic resource domain, unsolvable goal,
cyclic actions, and multiple cost paths where minimum action count and minimum
cost disagree. Expected answers come from ``tests.oracle`` (a separate BFS/
Dijkstra implementation), never from the planner under test.
"""

from __future__ import annotations

import math

import pytest

from strips_planner.core.executor import PlanExecutor
from strips_planner.core.search import SearchLimits, SearchStatus, run_search
from tests.oracle import exhaustive_solve

pytestmark = pytest.mark.unit

TIGHT_LIMITS = SearchLimits(max_expanded=10_000, max_depth=100, max_seconds=10.0)


@pytest.mark.parametrize("algorithm,heuristic", [
    ("bfs", "zero"),
    ("ucs", "zero"),
    ("astar", "hmax"),
    ("greedy", "goalcount"),
])
def test_found_plan_is_independently_executable(resource_problem, algorithm, heuristic) -> None:
    outcome = run_search(resource_problem, algorithm=algorithm, heuristic=heuristic,
                         limits=TIGHT_LIMITS)
    assert outcome.status == SearchStatus.FOUND
    trace = PlanExecutor(resource_problem).execute(list(outcome.plan))
    assert trace.valid, trace.message
    assert trace.total_cost == pytest.approx(outcome.path_cost)


def test_bfs_returns_minimum_action_count_which_is_expensive(resource_problem) -> None:
    # The goal needs the robot AT site2 AND crate_a STORED at site2.
    # Shortest plan uses the direct express link: pick (1) + express (9) +
    # drop (1) = 3 actions, cost 11. BFS must prefer it even though carrying
    # the crate through the cleared site1 route is cheaper (cost 9, 5 steps).
    outcome = run_search(resource_problem, algorithm="bfs", limits=TIGHT_LIMITS)
    oracle = exhaustive_solve(resource_problem)
    assert outcome.status == SearchStatus.FOUND
    assert outcome.optimal is True
    assert len(outcome.plan) == oracle.min_length == 3
    assert outcome.plan == (
        "(pick r1 crate_a depot)",
        "(express_move r1 depot site2)",
        "(drop r1 crate_a site2)",
    )
    assert outcome.path_cost == 11.0


def test_ucs_and_astar_return_cheapest_plan_with_more_actions(resource_problem) -> None:
    oracle = exhaustive_solve(resource_problem)
    # Oracle ground truth: cheapest costs 9 over 5 actions; shortest is 3/11.
    assert oracle.min_length == 3
    assert oracle.min_cost == 9.0
    assert oracle.min_length != len(oracle.cost_plan)

    for algorithm, heuristic in (("ucs", "zero"), ("astar", "hmax")):
        outcome = run_search(resource_problem, algorithm=algorithm,
                             heuristic=heuristic, limits=TIGHT_LIMITS)
        assert outcome.status == SearchStatus.FOUND
        assert outcome.optimal is True
        assert outcome.path_cost == pytest.approx(9.0)
        assert len(outcome.plan) == 5
        # clear (5) and pick at depot (1) are equal-cost-compatible orderings,
        # so assert the action multiset rather than one tie-break order.
        assert sorted(outcome.plan) == sorted([
            "(clear_block site1)",
            "(pick r1 crate_a depot)",
            "(move r1 depot site1)",
            "(move r1 site1 site2)",
            "(drop r1 crate_a site2)",
        ])
        trace = PlanExecutor(resource_problem).execute(list(outcome.plan))
        assert trace.valid


def test_astar_expands_no_more_states_than_ucs(resource_problem) -> None:
    ucs = run_search(resource_problem, algorithm="ucs", limits=TIGHT_LIMITS)
    astar = run_search(resource_problem, algorithm="astar", heuristic="hmax",
                       limits=TIGHT_LIMITS)
    assert astar.path_cost == pytest.approx(ucs.path_cost)
    assert astar.expanded <= ucs.expanded


def test_greedy_reports_no_optimality_promise(resource_problem) -> None:
    outcome = run_search(resource_problem, algorithm="greedy",
                         heuristic="goalcount", limits=TIGHT_LIMITS)
    assert outcome.status == SearchStatus.FOUND
    assert outcome.optimal is False
    # It still must return an executable plan regardless of cost.
    assert PlanExecutor(resource_problem).execute(list(outcome.plan)).valid


def test_astar_rejects_non_admissible_heuristic(resource_problem) -> None:
    from strips_planner.errors import ComputationError
    with pytest.raises(ComputationError) as exc:
        run_search(resource_problem, algorithm="astar", heuristic="goalcount")
    assert exc.value.code == "NON_ADMISSIBLE_HEURISTIC"


def test_unsolvable_goal_exhausts_and_reports_unsolvable(unsolvable_problem) -> None:
    oracle = exhaustive_solve(unsolvable_problem)
    assert oracle.solvable is False
    assert oracle.reachable > 1  # really did explore, not trivially empty

    for algorithm, heuristic in (("bfs", "zero"), ("ucs", "zero"),
                                 ("astar", "hmax"), ("greedy", "goalcount")):
        outcome = run_search(unsolvable_problem, algorithm=algorithm,
                             heuristic=heuristic, limits=TIGHT_LIMITS)
        assert outcome.status == SearchStatus.UNSOLVABLE, (algorithm, outcome.reason)
        assert outcome.plan == ()
        assert outcome.path_cost == math.inf
        assert outcome.expanded >= 1


def test_cyclic_actions_dedupe_and_terminate(resource_problem) -> None:
    # Without state dedupe, depot<->site1<->site2 loops re-expand forever; the
    # bitmask identity collapses them. The number of expanded states must not
    # exceed the independently enumerated reachable-state count.
    oracle = exhaustive_solve(resource_problem)
    outcome = run_search(resource_problem, algorithm="bfs",
                         limits=SearchLimits(max_expanded=5_000, max_depth=20,
                                             max_seconds=10.0))
    assert outcome.status == SearchStatus.FOUND
    assert outcome.expanded <= oracle.reachable
    # Revisits (edges into known states) do not create second expansions.
    assert outcome.reopened == 0


def test_depth_limit_returns_unknown_not_failure(resource_problem) -> None:
    # Every solution needs at least 3 actions; cutting at depth 1 must return
    # LIMIT with solvability unknown.
    outcome = run_search(resource_problem, algorithm="astar", heuristic="hmax",
                         limits=SearchLimits(max_expanded=10_000, max_depth=1,
                                             max_seconds=10.0))
    assert outcome.status == SearchStatus.LIMIT
    assert outcome.limit == "max_depth"
    assert outcome.optimal is False
    assert outcome.plan == ()
    assert "UNKNOWN" in outcome.reason


def test_expanded_limit_returns_bound_and_value(resource_problem) -> None:
    outcome = run_search(resource_problem, algorithm="ucs",
                         limits=SearchLimits(max_expanded=1, max_depth=10,
                                             max_seconds=10.0))
    assert outcome.status == SearchStatus.LIMIT
    assert outcome.limit == "max_expanded"
    assert outcome.limit_value == 1.0


def test_search_matches_oracle_on_all_engines(resource_problem) -> None:
    oracle = exhaustive_solve(resource_problem)
    bfs = run_search(resource_problem, algorithm="bfs", limits=TIGHT_LIMITS)
    astar = run_search(resource_problem, algorithm="astar", heuristic="hmax",
                       limits=TIGHT_LIMITS)
    assert len(bfs.plan) == oracle.min_length
    assert astar.path_cost == pytest.approx(oracle.min_cost)
    # Oracle's independently reconstructed plans must also execute.
    assert PlanExecutor(resource_problem).execute(list(oracle.length_plan)).valid
    assert PlanExecutor(resource_problem).execute(list(oracle.cost_plan)).valid


def test_initial_state_goal_is_empty_plan(resource_problem) -> None:
    from strips_planner.models import Problem
    trivial = Problem(
        name="already-done",
        types=resource_problem.types,
        objects=resource_problem.objects,
        predicates=resource_problem.predicates,
        init=resource_problem.init,
        goal_pos=frozenset(),
        goal_neg=frozenset(),
        schemas=resource_problem.schemas,
        ground_actions=resource_problem.ground_actions,
    )
    outcome = run_search(trivial, algorithm="astar", heuristic="hmax")
    assert outcome.status == SearchStatus.FOUND
    assert outcome.plan == ()
    assert outcome.path_cost == 0.0
    assert outcome.optimal is True
