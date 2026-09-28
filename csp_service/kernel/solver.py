"""Backtracking search with explicit trail restore and budgets.

Restore discipline: before trying a branch value, the solver pushes a
snapshot (domains, propagation queue) onto the trail; after the child
subtree returns, the snapshot is popped and restored in place. Every domain
and the queue therefore return bit-for-bit to their pre-branch state.

Terminal statuses are mutually exclusive:
- SAT:     search completed and >= 1 solution was found (mode=first stops at
           the first; mode=all lists every solution within budget)
- UNSAT:   search completed, no solution exists
- UNKNOWN: a budget (nodes or propagation steps) was exhausted first;
           partial solutions, if any, are flagged as partial
"""
from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from ..model import Problem
from .propagation import (
    BUDGET_EXCEEDED,
    INCONSISTENT,
    Propagator,
    QueueEntry,
    full_queue,
)

SAT = "SAT"
UNSAT = "UNSAT"
UNKNOWN = "UNKNOWN"


@dataclass
class SolveConfig:
    mode: str = "first"  # "first" | "all"
    max_nodes: int = 10_000
    max_propagation_steps: int = 100_000
    max_solutions: int | None = None


@dataclass
class SolveResult:
    status: str
    solutions: list[dict[str, int]]
    stats: dict
    events: list[dict]
    partial: bool = False


class _BudgetExceeded(Exception):
    pass


class Solver:
    def __init__(self, problem: Problem, config: SolveConfig | None = None):
        if config is None:
            config = SolveConfig()
        if config.mode not in ("first", "all"):
            raise ValueError("mode must be 'first' or 'all'")
        self.problem = problem
        self.config = config
        self.events: list[dict] = []
        self._seq = itertools.count()
        self._node_ids = itertools.count()
        self._propagator = Propagator(problem, self._record)
        self._steps_used = 0

    def _record(self, kind: str, payload: dict) -> None:
        self.events.append({"seq": next(self._seq), "kind": kind, **payload})

    def solve(self) -> SolveResult:
        domains: dict[str, set[int]] = {
            v.name: set(v.domain) for v in self.problem.variables
        }
        queue: deque[QueueEntry] = full_queue(self.problem)
        solutions: list[dict[str, int]] = []
        stats = {
            "nodes": 0,
            "backtracks": 0,
            "max_depth": 0,
            "propagation_steps": 0,
            "prunings": 0,
        }
        # Trail of (domains, queue) snapshots; restored on backtrack.
        trail: list[tuple[dict[str, set[int]], deque[QueueEntry]]] = []
        interrupted = False

        def propagate(node_id: int, depth: int) -> str:
            outcome = self._propagator.propagate(
                domains, queue, self.config.max_propagation_steps - self._steps_used,
                {"node": node_id, "depth": depth},
            )
            self._steps_used += outcome.revisions
            stats["propagation_steps"] = self._steps_used
            stats["prunings"] += len(outcome.pruned)
            return outcome.status

        def search(node_id: int, depth: int) -> None:
            stats["max_depth"] = max(stats["max_depth"], depth)
            status = propagate(node_id, depth)
            if status == INCONSISTENT:
                return
            if status == BUDGET_EXCEEDED:
                self._record("budget_exceeded", {"node": node_id, "depth": depth,
                                                 "budget": "propagation_steps"})
                raise _BudgetExceeded
            if all(len(d) == 1 for d in domains.values()):
                assignment = {k: next(iter(d)) for k, d in sorted(domains.items())}
                solutions.append(assignment)
                self._record("solution", {"node": node_id, "depth": depth,
                                          "assignment": assignment})
                return
            if stats["nodes"] >= self.config.max_nodes:
                self._record("budget_exceeded", {"node": node_id, "depth": depth,
                                                 "budget": "nodes"})
                raise _BudgetExceeded
            if (self.config.max_solutions is not None
                    and len(solutions) >= self.config.max_solutions):
                return

            stats["nodes"] += 1
            var = min(
                (v for v in domains if len(domains[v]) > 1),
                key=lambda v: (len(domains[v]), v),
            )
            for value in sorted(domains[var]):
                child_id = next(self._node_ids)
                # Snapshot domains AND queue; restored after the subtree.
                trail.append(({k: set(d) for k, d in domains.items()}, deque(queue)))
                domains[var] = {value}
                queue.extend(self._propagator.entries_for(var))
                self._record("branch", {"node": child_id, "parent": node_id,
                                        "depth": depth, "var": var, "value": value})
                search(child_id, depth + 1)
                saved_domains, saved_queue = trail.pop()
                domains.clear()
                domains.update(saved_domains)
                queue.clear()
                queue.extend(saved_queue)
                stats["backtracks"] += 1
                self._record("backtrack", {"node": node_id, "depth": depth,
                                           "from_child": child_id})
                if self.config.mode == "first" and solutions:
                    return
                if (self.config.max_solutions is not None
                        and len(solutions) >= self.config.max_solutions):
                    return

        try:
            search(next(self._node_ids), 0)
        except _BudgetExceeded:
            interrupted = True

        if interrupted:
            status = UNKNOWN
        elif solutions:
            status = SAT
        else:
            status = UNSAT
        self._record("status", {"status": status, "solutions": len(solutions)})
        return SolveResult(
            status=status,
            solutions=solutions,
            stats=stats,
            events=self.events,
            partial=interrupted,
        )
