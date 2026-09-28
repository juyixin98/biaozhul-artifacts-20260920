"""Relaxed-plan heuristics over the delete-relaxation.

Optimality contract (do not blur this):

* :class:`ZeroHeuristic`          - exact cost accounting; A* becomes
                                    uniform-cost search; optimal.
* :class:`MaxHeuristic` (h_max)   - admissible and consistent for positive
                                    STRIPS costs (max-propagation); negative
                                    preconditions are ignored by the
                                    relaxation, which stays optimistic; A*
                                    returns cost-optimal plans.
* :class:`AddHeuristic` (h_add)   - sums instead of taking the max; it is
                                    NOT admissible. Plans found with h_add
                                    are valid (the executor proves that) but
                                    are NOT promised optimal.

Every heuristic returns ``INF`` for a recognised dead end; the caller keeps
those nodes (so exhaustive unsolvability proofs remain valid) but expands
finite-valued nodes first.
"""

from __future__ import annotations

from .model import GroundAction, State

INF = float("inf")


class ZeroHeuristic:
    name = "h_zero"
    admissible = True

    def __init__(self, ground_problem) -> None:
        self.goal_pos = ground_problem.goal_pos

    def value(self, state: State):
        return 0

    def goal_remaining(self, state: State):
        return sorted(self.goal_pos - state)


class RelaxedCostHeuristic:
    """Shared forward cost propagation for h_max / h_add."""

    def __init__(self, ground_problem, mode: str) -> None:
        assert mode in ("max", "add")
        self.mode = mode
        self.name = f"h_{mode}"
        # h_max is admissible; h_add is not.
        self.admissible = mode == "max"
        self.goal_pos = ground_problem.goal_pos
        self.goal_neg = ground_problem.goal_neg
        self.actions: tuple[GroundAction, ...] = ground_problem.actions

        # fact -> actions that can add it
        self.achievers: dict = {}
        facts: set = set()
        for action in self.actions:
            for atom in action.add_effects:
                self.achievers.setdefault(atom, []).append(action)
                facts.add(atom)
            facts.update(action.pre_pos)
        facts.update(ground_problem.initial)
        facts.update(self.goal_pos)
        self.facts = facts

    def value(self, state: State):
        delta = {fact: (0 if fact in state else INF) for fact in self.facts}
        # Value iteration over positive-cost achievers converges in at most
        # |facts| sweeps (each sweep strictly improves some shortest support).
        for _ in range(len(self.facts) + 1):
            changed = False
            for action in self.actions:
                if not action.add_effects:
                    continue
                pre_values = [delta.get(p, INF) for p in action.pre_pos]
                if not pre_values:
                    base = 0
                elif any(v == INF for v in pre_values):
                    continue
                elif self.mode == "max":
                    base = max(pre_values)
                else:
                    base = sum(pre_values)
                candidate = base + action.cost
                for atom in action.add_effects:
                    if candidate < delta.get(atom, INF):
                        delta[atom] = candidate
                        changed = True
            if not changed:
                break

        goal_values = [delta.get(g, INF) for g in self.goal_pos]
        if not goal_values:
            return 0
        if any(v == INF for v in goal_values):
            return INF
        return max(goal_values) if self.mode == "max" else sum(goal_values)

    def goal_remaining(self, state: State):
        return sorted(self.goal_pos - state)


class MaxHeuristic(RelaxedCostHeuristic):
    def __init__(self, ground_problem) -> None:
        super().__init__(ground_problem, "max")


class AddHeuristic(RelaxedCostHeuristic):
    def __init__(self, ground_problem) -> None:
        super().__init__(ground_problem, "add")


HEURISTICS = {
    "zero": ZeroHeuristic,
    "h_max": MaxHeuristic,
    "h_add": AddHeuristic,
}
