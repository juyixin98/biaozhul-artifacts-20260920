"""Search kernel: verdicts, bounds, dedup and optimality promises."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from strips_planner.parser import parse_domain, parse_problem
from strips_planner.search import (
    ASTAR,
    BFS,
    DEPTH_LIMIT,
    NODE_LIMIT,
    UCS,
    SearchConfig,
    search,
)
from strips_planner.validation import validate
from strips_planner.grounding import ground

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _grounded(domain_file, problem_file):
    domain = parse_domain(json.loads((FIXTURES / domain_file).read_text()))
    problem = parse_problem(json.loads((FIXTURES / problem_file).read_text()), domain)
    validate(domain, problem)
    return ground(domain, problem)


ALL_CONFIGS = [
    (BFS, "zero"),
    (UCS, "zero"),
    (ASTAR, "h_max"),
    (ASTAR, "h_add"),
]


@pytest.mark.parametrize("algorithm,heuristic", ALL_CONFIGS)
def test_solvable_fixture_is_solved_and_verified_shape(algorithm, heuristic):
    gp = _grounded("domain_resource_ops.json", "problem_solvable.json")
    result = search(gp, SearchConfig(algorithm=algorithm, heuristic=heuristic))
    assert result.status == "solved"
    assert result.cost == 8
    assert result.path_length == 6
    assert [a.schema_name for a in result.plan] == [
        "move", "press", "move", "heat", "move", "ship"
    ]


# Generous wall-clock bound for tests whose assertion requires the reachable
# space to be fully exhausted; machine speed/coverage must not turn an
# exhaustion result into "unknown". Bound-triggering is tested separately
# with deliberately tiny limits below.
EXHAUSTIVE_TIME = 120.0


@pytest.mark.parametrize("algorithm,heuristic", [
    (BFS, "zero"), (UCS, "zero"), (ASTAR, "h_max"), (ASTAR, "h_add"),
])
def test_unreachable_predicate_is_provably_unsolvable(algorithm, heuristic):
    gp = _grounded(
        "domain_resource_ops.json", "problem_unsolvable_missing.json"
    )
    result = search(gp, SearchConfig(
        algorithm=algorithm, heuristic=heuristic,
        time_limit_seconds=EXHAUSTIVE_TIME,
    ))
    assert result.status == "unsolvable"
    assert result.plan == ()
    assert result.cost is None
    # Exhausted search must actually have explored the reachable component.
    assert result.expanded > 100


def test_sealed_dock_is_unsolvable_via_negative_precondition():
    gp = _grounded(
        "domain_resource_ops.json", "problem_unsolvable_sealed.json"
    )
    result = search(gp, SearchConfig(
        algorithm=ASTAR, heuristic="h_max",
        time_limit_seconds=EXHAUSTIVE_TIME,
    ))
    assert result.status == "unsolvable"
    assert result.reason is None


def test_cycle_actions_do_not_inflate_search_and_goal_is_reached():
    gp = _grounded("domain_resource_ops.json", "problem_cycles.json")
    result = search(gp, SearchConfig(algorithm=ASTAR, heuristic="h_max"))
    assert result.status == "solved"
    # Dedup collapses toggle/cut cycles: solution in 2 expansions.
    assert result.expanded == 3
    assert result.cost == 3
    assert [a.schema_name for a in result.plan] == ["move", "press"]


def test_bfs_prefers_fewest_hops_even_when_expensive():
    gp = _grounded("domain_route_graph.json", "problem_route_cost.json")
    bfs = search(gp, SearchConfig(algorithm=BFS, heuristic="zero"))
    assert bfs.path_length == 1
    assert bfs.cost == 5            # teleport: shortest hop count, higher cost
    assert bfs.optimal_guarantee is True  # length-optimal, not cost-optimal


def test_ucs_and_admissible_astar_return_cheapest_plan():
    gp = _grounded("domain_route_graph.json", "problem_route_cost.json")
    for algorithm, heuristic in [(UCS, "zero"), (ASTAR, "h_max")]:
        result = search(gp, SearchConfig(algorithm=algorithm,
                                         heuristic=heuristic))
        assert result.cost == 3
        assert result.path_length == 3
        assert [a.schema_name for a in result.plan] == ["walk"] * 3
        assert result.optimal_guarantee is True


def test_h_add_is_explicitly_not_optimal_promise():
    gp = _grounded("domain_route_graph.json", "problem_route_cost.json")
    result = search(gp, SearchConfig(algorithm=ASTAR, heuristic="h_add"))
    assert result.optimal_guarantee is False
    # Plan is still returned and must be re-verified by the executor elsewhere.
    assert result.status == "solved"


def test_node_limit_returns_unknown_with_reason_not_unsolvable():
    gp = _grounded(
        "domain_resource_ops.json", "problem_unsolvable_missing.json"
    )
    result = search(gp, SearchConfig(
        algorithm=ASTAR, heuristic="h_max", max_expansions=5,
    ))
    assert result.status == "unknown"
    assert result.reason == NODE_LIMIT
    assert result.expanded == 5
    assert result.plan == ()


def test_depth_limit_returns_unknown_when_space_is_truncated():
    gp = _grounded("domain_resource_ops.json", "problem_solvable.json")
    result = search(gp, SearchConfig(
        algorithm=BFS, heuristic="zero", max_depth=1,
    ))
    # Goal needs 6 steps; depth 1 cannot settle the question.
    assert result.status == "unknown"
    assert result.reason == DEPTH_LIMIT


def test_depth_limit_still_finds_shallow_solution():
    gp = _grounded("domain_route_graph.json", "problem_route_cost.json")
    result = search(gp, SearchConfig(
        algorithm=BFS, heuristic="zero", max_depth=1,
    ))
    assert result.status == "solved"
    assert result.path_length == 1


def test_time_limit_returns_unknown():
    gp = _grounded(
        "domain_resource_ops.json", "problem_unsolvable_missing.json"
    )
    result = search(gp, SearchConfig(
        algorithm=ASTAR, heuristic="h_max", time_limit_seconds=0.0001,
    ))
    assert result.status == "unknown"
    assert result.reason == "time_limit"


def test_state_dedup_collapses_repeated_generation():
    gp = _grounded("domain_resource_ops.json", "problem_cycles.json")
    result = search(gp, SearchConfig(algorithm=BFS, heuristic="zero"))
    # Each distinct state is expanded at most once despite toggle loops.
    hashes = [t.state_hash for t in result.trace]
    assert len(hashes) == len(set(hashes))


def test_trace_carries_intermediate_judgement_data():
    gp = _grounded("domain_route_graph.json", "problem_route_cost.json")
    result = search(gp, SearchConfig(algorithm=ASTAR, heuristic="h_max"))
    first = result.trace[0]
    assert first.seq == 1
    assert first.depth == 0
    assert first.g == 0
    assert first.h is not None
    assert first.f == first.h
    assert first.state_hash
