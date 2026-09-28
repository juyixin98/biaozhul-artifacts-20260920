"""Tests for heuristics, including an exhaustive admissibility check.

The admissibility property is not asserted from a value the planner produced:
the test enumerates every reachable state with the independent oracle's
Dijkstra labels and checks h(s) <= h*(s) for all of them (and h(goal)=0).
"""

from __future__ import annotations

import math

import pytest

from strips_planner.core.heuristics import HMax, h_goalcount, h_zero
from strips_planner.core.state import goal_satisfied, initial_state

pytestmark = pytest.mark.unit


def test_h_zero_is_zero_and_hmax_at_goal_is_zero(resource_problem) -> None:
    start = initial_state(resource_problem)
    assert h_zero(resource_problem, start) == 0.0
    hmax = HMax(resource_problem)
    goal_like = frozenset(resource_problem.goal_pos)
    assert hmax(resource_problem, goal_like) == 0.0


def test_hmax_is_infinite_for_unreachable_goal(unsolvable_problem) -> None:
    hmax = HMax(unsolvable_problem)
    start = initial_state(unsolvable_problem)
    assert hmax(unsolvable_problem, start) == math.inf


def test_goalcount_counts_unsatisfied_literals(resource_problem) -> None:
    start = initial_state(resource_problem)
    # Goal needs (stored crate_a site2) and (at r1 site2): 2 missing.
    assert h_goalcount(resource_problem, start) == 2.0


def test_hmax_is_admissible_at_every_reachable_state(resource_problem) -> None:
    from tests.oracle import goal_distances

    hmax = HMax(resource_problem)
    h_star = goal_distances(resource_problem)
    goal_states = [s for s in h_star if goal_satisfied(resource_problem, s)]
    assert len(h_star) > 5  # genuinely explored a space, not a trivial check
    assert goal_states
    violations = []
    for state, optimal_cost in h_star.items():
        h = hmax(resource_problem, state)
        if goal_satisfied(resource_problem, state):
            if h != 0.0:
                violations.append((sorted(map(str, state)), h, 0.0))
        elif not (h <= optimal_cost + 1e-9):
            violations.append((sorted(map(str, state)), h, optimal_cost))
    assert not violations, f"hmax overestimates: {violations[:3]}"
    # And informative: strictly positive at the initial state (true h* is 9).
    start = initial_state(resource_problem)
    assert 0 < hmax(resource_problem, start) < h_star[start]


def test_hmax_matches_hand_computed_relaxed_value(resource_problem) -> None:
    # Delete relaxation ignores negative preconditions, so the blocked site is
    # traversable: at(site2) costs 2 (two moves), carrying costs 1 (pick),
    # drop at site2 costs max(2, 1) + 1 = 3. Real optimum is 9, so h*=3 is a
    # strict, informative and admissible lower bound at the initial state.
    hmax = HMax(resource_problem)
    start = initial_state(resource_problem)
    assert hmax(resource_problem, start) == 3.0
