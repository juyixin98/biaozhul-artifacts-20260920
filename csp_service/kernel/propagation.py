"""Propagation engine: AC-3 style queue over binary tables + all-different.

The queue holds (constraint_id, changed_var) entries; changed_var=None means
"check the whole constraint" (used for initial propagation). Each revision
step consumes one unit of the propagation-step budget. Every pruned value is
reported with a machine-readable reason so the evidence store can answer
"why was this value removed".
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterable

from ..model import AllDifferentConstraint, Problem, TableConstraint
from .alldifferent import filter_all_different

QueueEntry = tuple[str, str | None]
RecordFn = Callable[[str, dict], None]

OK = "ok"
INCONSISTENT = "inconsistent"
BUDGET_EXCEEDED = "budget_exceeded"


@dataclass
class PropagationOutcome:
    status: str
    revisions: int = 0
    pruned: list[tuple[str, int]] = field(default_factory=list)


def full_queue(problem: Problem) -> deque[QueueEntry]:
    return deque((c.id, None) for c in problem.constraints)


class Propagator:
    def __init__(self, problem: Problem, record: RecordFn):
        self.problem = problem
        self.record = record
        self._constraints = {c.id: c for c in problem.constraints}
        self._by_var: dict[str, list[str]] = {v.name: [] for v in problem.variables}
        for c in problem.constraints:
            for var in c.vars:
                self._by_var[var].append(c.id)
        self._allowed: dict[str, frozenset[tuple[int, int]]] = {
            c.id: frozenset((p[0], p[1]) for p in c.allowed)
            for c in problem.constraints
            if isinstance(c, TableConstraint)
        }

    def entries_for(self, var: str) -> list[QueueEntry]:
        """Queue entries that must be re-checked when `var` changes."""
        return [(cid, var) for cid in self._by_var[var]]

    def propagate(
        self,
        domains: dict[str, set[int]],
        queue: deque[QueueEntry],
        step_budget: int,
        context: dict,
    ) -> PropagationOutcome:
        """Run to fixpoint. Mutates `domains`; drains `queue`.

        `context` carries node_id/depth stamped onto emitted events.
        """
        outcome = PropagationOutcome(status=OK)
        pending: set[QueueEntry] = set(queue)
        while queue:
            if outcome.revisions >= step_budget:
                outcome.status = BUDGET_EXCEEDED
                return outcome
            cid, changed = queue.popleft()
            pending.discard((cid, changed))
            constraint = self._constraints[cid]
            outcome.revisions += 1
            if isinstance(constraint, TableConstraint):
                removals = self._revise_table(constraint, changed, domains, context)
            else:
                removals = self._revise_all_different(constraint, domains, context)
            for var, value in removals:
                if value not in domains[var]:
                    continue
                domains[var].discard(value)
                outcome.pruned.append((var, value))
                if not domains[var]:
                    self.record("wipeout", {**context, "var": var, "constraint": cid})
                    outcome.status = INCONSISTENT
                    queue.clear()
                    return outcome
                for next_cid in self._by_var[var]:
                    entry = (next_cid, var)
                    if entry not in pending:
                        pending.add(entry)
                        queue.append(entry)
        return outcome

    def _revise_table(
        self,
        constraint: TableConstraint,
        changed: str | None,
        domains: dict[str, set[int]],
        context: dict,
    ) -> list[tuple[str, int]]:
        x, y = constraint.vars
        allowed = self._allowed[constraint.id]
        removals: list[tuple[str, int]] = []
        # Revise the side(s) opposite to the changed var; None => both.
        targets = [x] if changed == y else [y] if changed == x else [x, y]
        for target in targets:
            other = y if target == x else x
            flip = target == y  # allowed pairs are stored as (x, y)
            other_domain = sorted(domains[other])
            for value in sorted(domains[target]):
                supported = any(
                    (value, o) in allowed if not flip else (o, value) in allowed
                    for o in other_domain
                )
                if not supported:
                    removals.append((target, value))
                    self.record(
                        "prune",
                        {
                            **context,
                            "var": target,
                            "value": value,
                            "reason": {
                                "kind": "arc_no_support",
                                "constraint": constraint.id,
                                "other_var": other,
                                "other_domain": other_domain,
                            },
                        },
                    )
        return removals

    def _revise_all_different(
        self,
        constraint: AllDifferentConstraint,
        domains: dict[str, set[int]],
        context: dict,
    ) -> list[tuple[str, int]]:
        frozen = {v: frozenset(domains[v]) for v in constraint.vars}
        result = filter_all_different(constraint.vars, frozen)
        if not result.consistent:
            self.record(
                "hall_violation",
                {
                    **context,
                    "constraint": constraint.id,
                    "vars": result.hall.vars,
                    "values": result.hall.values,
                    "matching": result.matching,
                },
            )
            # Empty the domain of one Hall variable to force wipeout upstream.
            return [(result.hall.vars[0], v) for v in sorted(domains[result.hall.vars[0]])]
        removals = []
        for var, value in result.removals:
            removals.append((var, value))
            self.record(
                "prune",
                {
                    **context,
                    "var": var,
                    "value": value,
                    "reason": {
                        "kind": "alldifferent_no_max_matching",
                        "constraint": constraint.id,
                        "matching": {k: v for k, v in sorted(result.matching.items())},
                    },
                },
            )
        return removals
