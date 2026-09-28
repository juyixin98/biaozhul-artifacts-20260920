"""Budgeted chronological DFS solver.

Search strategy (deliberately different in structure from the flat
multiset brute force in :mod:`app.planner.enumerate`):

1. *Find any feasible plan* -- chronological DFS over grounded
   placements, sorted by ``(start, declaration index, duration)``.
   Schedules are multisets of placements (indices chosen non-decreasing,
   repetition allowed), so every schedule reachable under the occurrence
   cap is enumerated exactly once.
2. *Prove optimality* -- makespan bounds ``0, 1, ..., best-1`` are
   exhaustively searched in ascending order with the same DFS. Finishing
   bound ``b`` without a plan proves no plan with makespan ``<= b``
   exists; the first bound that yields a plan is therefore optimal.

A single shared **node-expansion budget** covers both phases:

* budget expires while still looking for any feasible plan
  -> ``NO_PLAN_WITHIN_BUDGET`` (feasibility unknown; never reported as a
     success);
* a feasible plan is known and the budget expires while an optimality
  bound is unfinished
  -> ``FEASIBLE_UNPROVEN``: the feasible plan is returned together with
     ``optimal=False`` and ``reason`` names the unfinished bound and the
     bounds already proven infeasible;
* the proof completes -> ``OPTIMAL``;
* the complete search space (under explicit occurrence/step caps) is
  exhausted without a plan -> ``INFEASIBLE``.

Incremental pruning only discards *irrevocably broken* prefixes:
half-open resource overlaps, same-instant write/write clashes, and
defects located strictly before the next placement's start time (closed
times that no future placement can affect). Every reported plan is
additionally re-verified end to end by :func:`replay`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from ..rules.conditions import apply_effects, effect_facts, evaluate
from ..rules.models import (
    Plan,
    Problem,
    ScheduledAction,
    SearchResult,
    SearchStatus,
)
from ..rules.time import Interval, overlaps
from .replay import replay


class _BudgetExceeded(Exception):
    pass


@dataclass(frozen=True)
class _Placement:
    start: int
    decl_index: int
    duration: int

    @property
    def end(self) -> int:
        return self.start + self.duration

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.start, self.decl_index, self.duration)


@dataclass(frozen=True)
class SolverConfig:
    budget_nodes: int = 200_000
    max_steps: int = 32
    max_occurrences_per_action: int | None = None  # default: horizon + 2


class _Budget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.expanded = 0

    def tick(self) -> None:
        self.expanded += 1
        if self.expanded > self.limit:
            raise _BudgetExceeded


def _placements(problem: Problem, makespan_limit: int) -> list[_Placement]:
    placements: list[_Placement] = []
    for decl_index, action in enumerate(problem.actions):
        for duration in action.duration_choices:
            for start in range(0, makespan_limit - duration + 1):
                placements.append(_Placement(start, decl_index, duration))
    return sorted(placements, key=lambda p: p.key)


def _to_plan(problem: Problem, chosen: list[_Placement]) -> Plan:
    return Plan(
        steps=[
            ScheduledAction(
                action=problem.actions[p.decl_index].name,
                start=p.start,
                duration=p.duration,
            )
            for p in chosen
        ]
    )


def _plan_makespan(chosen: list[_Placement]) -> int:
    return max((p.end for p in chosen), default=0)


def _resources_pairwise_ok(problem: Problem, chosen: list[_Placement], candidate: _Placement) -> bool:
    if candidate.duration == 0:
        return True  # [t, t) holds nothing
    new_interval = Interval(candidate.start, candidate.end)
    candidate_resources = set(problem.actions[candidate.decl_index].resources)
    if not candidate_resources:
        return True
    for other in chosen:
        if other.duration == 0:
            continue
        shared = candidate_resources & set(problem.actions[other.decl_index].resources)
        if shared and overlaps(Interval(other.start, other.end), new_interval):
            return False
    return True


def _writes_pairwise_ok(problem: Problem, chosen: list[_Placement], candidate: _Placement) -> bool:
    candidate_when = candidate.start if candidate.duration == 0 else candidate.end
    candidate_facts = effect_facts(problem.actions[candidate.decl_index].effects)
    if not candidate_facts:
        return True
    for other in chosen:
        other_when = other.start if other.duration == 0 else other.end
        if other_when != candidate_when:
            continue
        if candidate_facts & effect_facts(problem.actions[other.decl_index].effects):
            return False
    return True


def _closed_times_ok(problem: Problem, chosen: list[_Placement], frontier: int) -> bool:
    """Replay phases for committed times ``0 .. frontier-1`` (sound pruning).

    Placements are appended in non-decreasing start order. Every event at
    a time ``t < frontier`` is fully known (remaining placements start at
    ``>= frontier > t`` and can neither end at t, start at t nor be
    active on segment t), so any violation found there is irrevocable.
    Mirrors the replay order: END -> PRE -> ZERO -> segment invariants.
    """
    if frontier <= 0:
        return True
    by_end: dict[int, list[_Placement]] = {}
    by_start: dict[int, list[_Placement]] = {}
    for placement in chosen:
        if placement.duration > 0:
            by_end.setdefault(placement.end, []).append(placement)
        by_start.setdefault(placement.start, []).append(placement)

    state: dict[str, object] = dict(problem.initial)
    for t in range(0, frontier):
        for placement in sorted(by_end.get(t, []), key=lambda p: p.decl_index):
            state = apply_effects(problem.actions[placement.decl_index].effects, state)
        starters = sorted(by_start.get(t, []), key=lambda p: p.decl_index)
        for placement in starters:
            if not evaluate(problem.actions[placement.decl_index].precondition, state):
                return False
        for placement in starters:
            if placement.duration == 0:
                state = apply_effects(problem.actions[placement.decl_index].effects, state)
        for placement in chosen:
            if placement.duration > 0 and placement.start <= t < placement.end:
                if not evaluate(problem.actions[placement.decl_index].invariant, state):
                    return False
    return True


def solve(problem: Problem, config: Optional[SolverConfig] = None) -> SearchResult:
    """Search for a minimum-makespan plan under a node-expansion budget."""
    cfg = config or SolverConfig()
    started = time.perf_counter()
    horizon = problem.horizon
    occurrence_cap = cfg.max_occurrences_per_action or horizon + 2
    budget = _Budget(cfg.budget_nodes)
    deepest = 0

    def elapsed_ms() -> float:
        return round((time.perf_counter() - started) * 1000.0, 3)

    def result(
        status: SearchStatus,
        plan: list[_Placement] | None,
        reason: str,
        *,
        optimal: bool,
    ) -> SearchResult:
        return SearchResult(
            status=status,
            plan=_to_plan(problem, plan) if plan is not None else None,
            makespan=_plan_makespan(plan) if plan is not None else None,
            nodes_expanded=budget.expanded,
            budget_nodes=cfg.budget_nodes,
            elapsed_ms=elapsed_ms(),
            optimal=optimal,
            reason=reason,
            explored_depth=deepest,
        )

    def dfs(
        placements: list[_Placement],
        chosen: list[_Placement],
        from_index: int,
        counts: dict[int, int],
    ) -> list[_Placement] | None:
        """Depth-first multiset enumeration; first replay-valid node wins."""
        nonlocal deepest
        budget.tick()
        deepest = max(deepest, len(chosen))
        if chosen and replay(problem, _to_plan(problem, chosen)).is_valid:
            return list(chosen)
        if len(chosen) >= cfg.max_steps:
            return None
        for index in range(from_index, len(placements)):
            candidate = placements[index]
            if counts.get(candidate.decl_index, 0) >= occurrence_cap:
                continue
            if not _resources_pairwise_ok(problem, chosen, candidate):
                continue
            if not _writes_pairwise_ok(problem, chosen, candidate):
                continue
            chosen.append(candidate)
            counts[candidate.decl_index] = counts.get(candidate.decl_index, 0) + 1
            if _closed_times_ok(problem, chosen, candidate.start):
                found = dfs(placements, chosen, index, counts)
                if found is not None:
                    return found
            chosen.pop()
            counts[candidate.decl_index] -= 1
        return None

    # Empty plan is a legal answer whenever the goal holds initially.
    empty_valid = replay(problem, Plan(steps=[])).is_valid

    # ---- Phase 1: find any feasible plan within budget -----------------
    best: list[_Placement] | None = [] if empty_valid else None
    if best is None:
        try:
            best = dfs(_placements(problem, horizon), [], 0, {})
        except _BudgetExceeded:
            return result(
                SearchStatus.NO_PLAN_WITHIN_BUDGET,
                None,
                f"node budget {cfg.budget_nodes} exhausted while searching for any feasible "
                f"plan (nodes expanded: {budget.expanded}); feasibility is unproven, not denied",
                optimal=False,
            )
        if best is None:
            return result(
                SearchStatus.INFEASIBLE,
                None,
                f"search space exhausted with no feasible plan (horizon={horizon}, "
                f"max_steps={cfg.max_steps}, occurrence_cap={occurrence_cap})",
                optimal=False,
            )

    # ---- Phase 2: prove optimality, bound by bound (shared budget) -----
    # Ascending bounds: the first bound b at which a plan exists, after
    # bounds 0..b-1 were exhausted without one, is the optimum.
    best_makespan = _plan_makespan(best)
    for bound in range(0, best_makespan):
        try:
            smaller = dfs(_placements(problem, bound), [], 0, {})
        except _BudgetExceeded:
            proven = list(range(0, bound))
            return result(
                SearchStatus.FEASIBLE_UNPROVEN,
                best,
                f"feasible plan with makespan {best_makespan} found, but the node budget "
                f"{cfg.budget_nodes} expired while exhaustively proving bound {bound}; "
                f"bounds {proven} were already proven infeasible, so optimality is unproven",
                optimal=False,
            )
        if smaller is not None:
            # All bounds below already exhausted without a plan, so the
            # first plan found at this ascending bound is the optimum.
            best = smaller
            best_makespan = _plan_makespan(smaller)
            break

    # Defence in depth: the returned plan must pass the independent gate.
    verified = replay(problem, _to_plan(problem, best))
    if not verified.is_valid:
        return result(
            SearchStatus.NO_PLAN_WITHIN_BUDGET,
            None,
            "internal verification rejected the candidate plan; reporting no plan rather "
            "than an unverified success",
            optimal=False,
        )
    return result(
        SearchStatus.OPTIMAL,
        best,
        f"optimal plan: makespan {best_makespan}; all bounds 0..{best_makespan - 1} "
        f"exhausted without a feasible plan",
        optimal=True,
    )
