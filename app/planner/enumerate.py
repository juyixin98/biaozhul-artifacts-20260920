"""Small-grid exhaustive reference enumerator.

This is the deliberately simple, *trustworthy* reference: it performs a
flat exhaustive enumeration of bounded multisets of grounded placements
and asks the replay engine to judge each one. The only pruning it does is
purely structural and therefore completeness-preserving: placements that
already overlap a chosen one on a half-open resource interval, or that
write the same fact at the same instant as a chosen placement, can never
be accepted by replay, so they are skipped. It deliberately does NOT do
the solver's incremental chronological state simulation -- its trust
comes from keeping the search close to the raw schedule space.

It is intentionally separate from :mod:`app.planner.solver`, whose
chronological DFS prunes incrementally and honours a search budget.
Tests cross-check both implementations against each other and against a
third, test-local oracle.

A grounded placement is a tuple ``(start, decl_index, duration)``.
Schedules are multisets of placements; to enumerate each multiset once,
placement indices are chosen in non-decreasing order (repetition
allowed, capped by ``max_occurrences_per_action``).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..rules.conditions import effect_facts
from ..rules.models import Plan, Problem, ScheduledAction
from ..rules.time import Interval, overlaps
from .replay import replay


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


def _structurally_compatible(problem: Problem, chosen: list[_Placement], candidate: _Placement) -> bool:
    """Completeness-preserving pairwise filter (resource + write clashes)."""
    action = problem.actions[candidate.decl_index]
    candidate_resources = set(action.resources)
    candidate_when = candidate.start if candidate.duration == 0 else candidate.end
    candidate_writes = effect_facts(action.effects)
    candidate_interval = Interval(candidate.start, candidate.end)
    for other in chosen:
        other_action = problem.actions[other.decl_index]
        if (
            candidate.duration > 0
            and other.duration > 0
            and candidate_resources
            and candidate_resources & set(other_action.resources)
            and overlaps(Interval(other.start, other.end), candidate_interval)
        ):
            return False
        other_when = other.start if other.duration == 0 else other.end
        if other_when == candidate_when and candidate_writes & effect_facts(other_action.effects):
            return False
    return True


def grounded_placements(problem: Problem, *, makespan_limit: int) -> list[_Placement]:
    """All placements with ``end <= makespan_limit``."""
    placements: list[_Placement] = []
    for decl_index, action in enumerate(problem.actions):
        for duration in action.duration_choices:
            latest_start = makespan_limit - duration
            for start in range(0, latest_start + 1):
                placements.append(_Placement(start=start, decl_index=decl_index, duration=duration))
    return sorted(placements, key=lambda p: p.key)


@dataclass(frozen=True)
class ReferenceAnswer:
    found: bool
    optimal_makespan: int | None
    plan: Plan | None
    schedules_evaluated: int


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


def exhaustive_reference(
    problem: Problem,
    *,
    max_steps: int,
    max_occurrences_per_action: int,
    makespan_limit: int | None = None,
) -> ReferenceAnswer:
    """Exhaustively find a minimum-makespan feasible plan on the small grid.

    Iterative deepening over the makespan bound: the first bound yielding
    a valid plan is optimal (every smaller bound was fully enumerated).

    ``max_steps`` bounds multiset size (without it, zero-duration actions
    would make the search space unbounded).
    """
    limit = problem.horizon if makespan_limit is None else makespan_limit
    evaluated = 0
    for bound in range(0, limit + 1):
        placements = grounded_placements(problem, makespan_limit=bound)
        # The empty multiset must be considered too (goal holds initially).
        best_at_bound: list[_Placement] | None = None

        def visit(chosen: list[_Placement], from_index: int, counts: dict[int, int]) -> None:
            nonlocal evaluated, best_at_bound
            evaluated += 1
            if best_at_bound is None and chosen:
                plan = _to_plan(problem, chosen)
                if replay(problem, plan).is_valid:
                    best_at_bound = list(chosen)
            if len(chosen) >= max_steps or from_index >= len(placements):
                return
            for index in range(from_index, len(placements)):
                placement = placements[index]
                if counts.get(placement.decl_index, 0) >= max_occurrences_per_action:
                    continue
                if not _structurally_compatible(problem, chosen, placement):
                    continue
                # Equal index may repeat (multiset); smaller indices never recur.
                chosen.append(placement)
                counts[placement.decl_index] = counts.get(placement.decl_index, 0) + 1
                visit(chosen, index, counts)
                chosen.pop()
                counts[placement.decl_index] -= 1

        visit([], 0, {})

        # Empty plan check at this bound (cheap; goal may hold initially).
        if best_at_bound is None and bound == 0 and replay(problem, Plan(steps=[])).is_valid:
            return ReferenceAnswer(True, 0, Plan(steps=[]), evaluated)

        if best_at_bound is not None:
            return ReferenceAnswer(True, bound, _to_plan(problem, best_at_bound), evaluated)

    return ReferenceAnswer(False, None, None, evaluated)
