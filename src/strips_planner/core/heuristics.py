"""Search heuristics with explicit admissibility claims.

The optimality promise of A* depends entirely on the heuristic, so each
heuristic documents its guarantee:

================  ===========  ===============================================
name             admissible?  meaning
================  ===========  ===============================================
``zero``         yes          h = 0; A* degenerates to uniform-cost.
``hmax``         yes          delete-relaxation h^max with action costs,
                              including an admissible bound for required
                              deletions implied by negative goals.
``goalcount``    no           number of unsatisfied goal literals; guides
                              greedy search only; a single action may fix
                              several literals at once, so it can overestimate.
================  ===========  ===============================================

h^max ignores *negative* preconditions (relaxing a constraint can only make
the relaxed problem easier, preserving admissibility). A negative goal literal
``(not p)`` whose atom ``p`` currently holds requires the real plan to include
some action deleting ``p``; we lower-bound that with the most expensive of the
cheapest required deletions and take the maximum with h^max (one action may
delete several such atoms, so a sum would not be admissible).
"""

from __future__ import annotations

import math
from typing import Callable

from strips_planner.core.state import State
from strips_planner.models import Problem

Heuristic = Callable[[Problem, State], float]
INF = math.inf


def h_zero(problem: Problem, state: State) -> float:
    return 0.0


def h_goalcount(problem: Problem, state: State) -> float:
    return float(len(problem.goal_pos - state) + len(problem.goal_neg & state))


class HMax:
    """Delete-relaxation h^max, precomputed per problem, evaluated per state."""

    def __init__(self, problem: Problem) -> None:
        self._problem = problem
        self._goals_pos = problem.goal_pos
        self._goals_neg = problem.goal_neg
        self._actions = problem.ground_actions

        # Cheapest deletion cost for each fluent required absent by the goal.
        cheapest_delete: dict = {}
        for action in self._actions:
            for atom in action.delete:
                prev = cheapest_delete.get(atom, INF)
                cheapest_delete[atom] = min(prev, action.cost)
        self._neg_goal_delete_cost = {
            atom: cheapest_delete.get(atom, INF) for atom in problem.goal_neg
        }

    def __call__(self, problem: Problem, state: State) -> float:
        dist: dict = {atom: 0.0 for atom in state}
        # Bellman-Ford-style fixpoint over the relaxed (no-delete) actions.
        # At most |fluents| relaxation rounds can improve a distance.
        for _ in range(self._fluents_bound()):
            changed = False
            for action in self._actions:
                requirement = 0.0
                achievable = True
                for pre in action.pre_pos:
                    d = dist.get(pre)
                    if d is None:
                        achievable = False
                        break
                    requirement = max(requirement, d)
                if not achievable:
                    continue
                candidate = requirement + action.cost
                for atom in action.add:
                    if candidate < dist.get(atom, INF):
                        dist[atom] = candidate
                        changed = True
            if not changed:
                break

        positive: float = 0.0
        for goal in self._goals_pos:
            d = dist.get(goal)
            if d is None:
                return INF  # relaxed problem cannot reach this goal
            positive = max(positive, d)

        negative_bound = 0.0
        for atom in self._goals_neg:
            if atom in state:
                cost = self._neg_goal_delete_cost.get(atom, INF)
                if cost == INF:
                    return INF  # nothing ever deletes this fluent
                negative_bound = max(negative_bound, cost)

        return max(positive, negative_bound)

    def _fluents_bound(self) -> int:
        fluents = set()
        for action in self._actions:
            fluents.update(action.add)
            fluents.update(action.pre_pos)
        fluents.update(self._goals_pos)
        return max(1, len(fluents))


HEURISTICS: dict[str, Callable[[Problem], Heuristic] | Heuristic] = {
    "zero": h_zero,
    "goalcount": h_goalcount,
    "hmax": HMax,
}


def make_heuristic(name: str, problem: Problem) -> tuple[Heuristic, bool]:
    """Return ``(heuristic, is_admissible)``; raise KeyError on unknown name."""
    factory = HEURISTICS[name]
    if factory is h_zero or factory is h_goalcount:
        return factory, factory is h_zero
    heuristic = factory(problem)
    return heuristic, True
