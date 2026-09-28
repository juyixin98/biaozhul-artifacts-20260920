"""Backtracking search over the propagation kernel.

The search assigns variables one at a time. Each branch:

1. copies the current trail length and prunes the branch assignment;
2. propagates to fixpoint;
3. on success recurses, on failure restores *all* domains by replaying the
   branch trail and continues with the next value.

Both the assignment prunings and every propagation pruning are trailed, so
backtracking restores the complete domain state. The propagation queue is
local to a single :func:`run_propagation` frame, hence discarded with the
branch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .domain import DomainStore, EmptyDomain
from .models import CSPModel
from .propagate import (
    AllDifferentUnsatisfiable,
    Propagator,
    PropagationStats,
    run_propagation,
)


class SearchStatus(str, Enum):
    SAT = "sat"  # a solution was found
    UNSAT = "unsat"  # proven infeasible
    UNKNOWN = "unknown"  # node/backtrack budget exhausted before deciding


class FailureKind(str, Enum):
    EMPTY_DOMAIN = "empty_domain"
    HALL_VIOLATION = "hall_violation"
    BUDGET = "budget"


@dataclass
class Branch:
    depth: int
    variable: str
    value: int
    status: str  # "propagated" | "rejected"
    restored: bool


@dataclass
class SearchTrace:
    steps: list[dict[str, Any]] = field(default_factory=list)
    branches: list[Branch] = field(default_factory=list)
    propagation_logs: list[dict[str, Any]] = field(default_factory=list)

    def log_propagation(self, event: dict[str, Any]) -> None:
        self.propagation_logs.append(event)


@dataclass
class SearchResult:
    status: SearchStatus
    solution: dict[str, int] | None
    domains: dict[str, list[int]]
    stats: dict[str, Any]
    reason_trace: list[dict[str, Any]]
    branches: list[dict[str, Any]]
    failure: dict[str, Any] | None = None


@dataclass
class _Counters:
    nodes: int = 0
    backtracks: int = 0
    prunes: int = 0
    binary_revisions: int = 0
    alldifferent_runs: int = 0


class Solver:
    """Depth-first search with arc consistency and Régin propagation."""

    def __init__(self, model: CSPModel):
        self.model = model
        self.propagator = Propagator(model)

    def solve(
        self,
        max_nodes: int = 100_000,
        max_backtracks: int = 100_000,
        collect_reasons: bool = True,
    ) -> SearchResult:
        store = DomainStore(self.model.domains)
        counters = _Counters()
        trace = SearchTrace()
        root_trail: list[Any] = []
        root_stats = PropagationStats()

        try:
            run_propagation(self.propagator, store, root_trail, root_stats, changed=None)
        except EmptyDomain as exc:
            return self._result(
                SearchStatus.UNSAT,
                None,
                store,
                counters,
                trace,
                root_stats,
                failure={
                    "kind": FailureKind.EMPTY_DOMAIN.value,
                    "variable": exc.variable,
                    "at": "root",
                },
            )
        except AllDifferentUnsatisfiable as exc:
            return self._result(
                SearchStatus.UNSAT,
                None,
                store,
                counters,
                trace,
                root_stats,
                failure={
                    "kind": FailureKind.HALL_VIOLATION.value,
                    "group_index": exc.group_index,
                    "hall_variables": exc.hall_vars,
                    "hall_values": exc.hall_values,
                    "at": "root",
                },
            )
        counters.binary_revisions += root_stats.binary_revisions
        counters.alldifferent_runs += root_stats.alldifferent_runs
        counters.prunes += root_stats.prunes
        trace.log_propagation(
            {
                "at": "root",
                "prunes": root_stats.prunes,
                "reasons": list(root_stats.steps) if collect_reasons else [],
            }
        )

        budget = Budget(max_nodes, max_backtracks)
        outcome = self._search(store, counters, trace, budget, depth=1,
                               collect_reasons=collect_reasons)

        if outcome.status is SearchStatus.SAT:
            solution = {var: next(iter(store.domain(var))) for var in store.variables()}
        else:
            solution = None
        return self._result(
            outcome.status,
            solution,
            store,
            counters,
            trace,
            root_stats,
            failure=outcome.failure,
        )

    # -- internals ----------------------------------------------------------

    def _search(
        self,
        store: DomainStore,
        counters: _Counters,
        trace: SearchTrace,
        budget: "Budget",
        depth: int,
        collect_reasons: bool,
    ) -> "_Outcome":
        if store.all_singletons():
            return _Outcome(SearchStatus.SAT, None)
        counters.nodes += 1
        if not budget.allow_node():
            return _Outcome(
                SearchStatus.UNKNOWN,
                {
                    "kind": FailureKind.BUDGET.value,
                    "limit": budget.description(),
                    "nodes": counters.nodes,
                    "backtracks": counters.backtracks,
                },
            )

        variable = self._select_variable(store)
        for value in sorted(store.domain(variable)):
            trail: list[Any] = []
            stats = PropagationStats()
            try:
                store.assign(variable, value, trail)
                run_propagation(
                    self.propagator, store, trail, stats, changed=[variable]
                )
            except EmptyDomain as exc:
                self._rollback(store, trail)
                trace.branches.append(
                    Branch(depth, variable, value, "rejected", restored=True)
                )
                counters.backtracks += 1
                trace.log_propagation(
                    {
                        "at": f"depth {depth}",
                        "assignment": [variable, value],
                        "rejected_by": FailureKind.EMPTY_DOMAIN.value,
                        "variable": exc.variable,
                        "prunes": stats.prunes,
                        "reasons": list(stats.steps) if collect_reasons else [],
                    }
                )
                if not budget.allow_backtrack():
                    return _Outcome(
                        SearchStatus.UNKNOWN,
                        {
                            "kind": FailureKind.BUDGET.value,
                            "limit": budget.description(),
                            "nodes": counters.nodes,
                            "backtracks": counters.backtracks,
                        },
                    )
                continue
            except AllDifferentUnsatisfiable as exc:
                self._rollback(store, trail)
                trace.branches.append(
                    Branch(depth, variable, value, "rejected", restored=True)
                )
                counters.backtracks += 1
                trace.log_propagation(
                    {
                        "at": f"depth {depth}",
                        "assignment": [variable, value],
                        "rejected_by": FailureKind.HALL_VIOLATION.value,
                        "group_index": exc.group_index,
                        "hall_variables": exc.hall_vars,
                        "hall_values": exc.hall_values,
                        "prunes": stats.prunes,
                        "reasons": list(stats.steps) if collect_reasons else [],
                    }
                )
                if not budget.allow_backtrack():
                    return _Outcome(
                        SearchStatus.UNKNOWN,
                        {
                            "kind": FailureKind.BUDGET.value,
                            "limit": budget.description(),
                            "nodes": counters.nodes,
                            "backtracks": counters.backtracks,
                        },
                    )
                continue

            counters.binary_revisions += stats.binary_revisions
            counters.alldifferent_runs += stats.alldifferent_runs
            counters.prunes += stats.prunes
            trace.branches.append(
                Branch(depth, variable, value, "propagated", restored=False)
            )
            trace.log_propagation(
                {
                    "at": f"depth {depth}",
                    "assignment": [variable, value],
                    "prunes": stats.prunes,
                    "reasons": list(stats.steps) if collect_reasons else [],
                }
            )

            outcome = self._search(
                store, counters, trace, budget, depth + 1, collect_reasons
            )
            if outcome.status is SearchStatus.SAT:
                # Keep the successful branch's domains; do not restore.
                return outcome
            self._rollback(store, trail)
            trace.branches[-1].restored = True
            if outcome.status is SearchStatus.UNKNOWN:
                return outcome
            counters.backtracks += 1
            if not budget.allow_backtrack():
                return _Outcome(
                    SearchStatus.UNKNOWN,
                    {
                        "kind": FailureKind.BUDGET.value,
                        "limit": budget.description(),
                        "nodes": counters.nodes,
                        "backtracks": counters.backtracks,
                    },
                )
        return _Outcome(SearchStatus.UNSAT, None)

    @staticmethod
    def _rollback(store: DomainStore, trail: list[Any]) -> None:
        """Restore every domain change recorded on this branch trail."""
        DomainStore.restore(trail, store)

    def _select_variable(self, store: DomainStore) -> str:
        """Minimum-domain (MRV) heuristic, ties broken by name."""
        candidates = [
            variable
            for variable in self.model.domains
            if len(store.domain(variable)) > 1
        ]
        return min(candidates, key=lambda var: (len(store.domain(var)), var))

    def _result(
        self,
        status: SearchStatus,
        solution: dict[str, int] | None,
        store: DomainStore,
        counters: _Counters,
        trace: SearchTrace,
        root_stats: PropagationStats,
        failure: dict[str, Any] | None,
    ) -> SearchResult:
        reasons: list[dict[str, Any]] = []
        if root_stats.steps:
            reasons.extend(root_stats.steps)
        for event in trace.propagation_logs:
            reasons.extend(event.get("reasons", []))
        return SearchResult(
            status=status,
            solution=solution,
            domains={var: sorted(store.domain(var)) for var in store.variables()},
            stats={
                "nodes": counters.nodes,
                "backtracks": counters.backtracks,
                "prunes": counters.prunes,
                "binary_revisions": counters.binary_revisions,
                "alldifferent_runs": counters.alldifferent_runs,
            },
            reason_trace=reasons,
            branches=[
                {
                    "depth": branch.depth,
                    "variable": branch.variable,
                    "value": branch.value,
                    "status": branch.status,
                    "restored": branch.restored,
                }
                for branch in trace.branches

            ],
            failure=failure,
        )


@dataclass
class _Outcome:
    status: SearchStatus
    failure: dict[str, Any] | None


class Budget:
    def __init__(self, max_nodes: int, max_backtracks: int):
        self.max_nodes = max_nodes
        self.max_backtracks = max_backtracks
        self.nodes_used = 0
        self.backtracks_used = 0

    def allow_node(self) -> bool:
        self.nodes_used += 1
        return self.nodes_used <= self.max_nodes

    def allow_backtrack(self) -> bool:
        self.backtracks_used += 1
        return self.backtracks_used <= self.max_backtracks

    def description(self) -> str:
        return f"max_nodes={self.max_nodes}, max_backtracks={self.max_backtracks}"
