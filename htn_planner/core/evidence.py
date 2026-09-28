"""Evidence data structures for planning outcomes.

The kernel never prints its reasoning: every decision, rejection and
dead-end is captured here so the API, persistence layer and verifier can
explain *why* a plan exists or why none exists.
"""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any

Atom = str | int | float


class Outcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    # Search was truncated by the engine safety budget: neither feasibility
    # nor infeasibility was established.
    INCONCLUSIVE = "inconclusive"


class FailureCategory(str, Enum):
    """Definitive failure categories (proved dead-ends within the horizon)."""

    NO_APPLICABLE_METHOD = "no_applicable_method"
    DEADLOCK = "deadlock"
    DEPTH_BUDGET = "depth_budget"
    EXPANSION_BUDGET = "expansion_budget"
    ACTION_BUDGET = "action_budget"
    CYCLE = "cycle"
    ARITY_MISMATCH = "arity_mismatch"
    SEARCH_BUDGET = "search_budget"


@dataclasses.dataclass(frozen=True)
class KeyStep:
    """One human-interpretably logged decision point."""

    seq: int
    name: str
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "name": self.name, "detail": self.detail}


@dataclasses.dataclass(frozen=True)
class FailureRecord:
    category: str
    task: list[Any] | None
    detail: str
    depth: int
    method_chain: tuple[str, ...]
    action_prefix: tuple[tuple[str, tuple[Atom, ...]], ...]
    rejected_methods: tuple[dict[str, Any], ...] = ()
    blocked: tuple[dict[str, Any], ...] = ()
    # Deadlock snapshots: the residual ground task network at the dead end so
    # an independent checker can replay every linearization itself.
    remaining_tasks: tuple[dict[str, Any], ...] = ()
    remaining_edges: tuple[list[str], ...] = ()
    # Cycle evidence: enclosing ground task expressions, outermost first, with
    # the repeated task appended last.
    cycle_chain: tuple[list[Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "task": self.task,
            "detail": self.detail,
            "depth": self.depth,
            "method_chain": list(self.method_chain),
            "action_prefix": [[name, list(args)] for name, args in self.action_prefix],
            "rejected_methods": list(self.rejected_methods),
            "blocked": list(self.blocked),
            "remaining_tasks": list(self.remaining_tasks),
            "remaining_edges": [list(e) for e in self.remaining_edges],
            "cycle_chain": [list(t) for t in self.cycle_chain],
        }


@dataclasses.dataclass(frozen=True)
class ActionStep:
    seq: int
    node_id: str
    operator: str
    args: tuple[Atom, ...]
    method_path: tuple[str, ...]
    added: tuple[tuple[Atom, ...], ...]
    deleted: tuple[tuple[Atom, ...], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "node_id": self.node_id,
            "operator": self.operator,
            "args": list(self.args),
            "method_path": list(self.method_path),
            "added": [list(f) for f in self.added],
            "deleted": [list(f) for f in self.deleted],
        }


@dataclasses.dataclass(frozen=True)
class ExpansionRecord:
    """One node of the successful abstract->primitive expansion tree."""

    node_id: str
    parent_id: str | None
    task: tuple[Atom, ...]
    kind: str  # "root" | "compound" | "primitive"
    depth: int
    expanded_by: str | None
    operator: str | None
    children: tuple[str, ...]
    # (child-index, child-index) precedence pairs among ``children``
    ordering: tuple[tuple[int, int], ...]
    ordered: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "parent_id": self.parent_id,
            "task": list(self.task),
            "kind": self.kind,
            "depth": self.depth,
            "expanded_by": self.expanded_by,
            "operator": self.operator,
            "children": list(self.children),
            "ordering": [list(p) for p in self.ordering],
            "ordered": self.ordered,
        }


@dataclasses.dataclass(frozen=True)
class PlanResult:
    request_id: str
    status: str
    domain_name: str
    problem_name: str
    engine_version: str
    domain_version: str
    bounds: dict[str, int]
    counters: dict[str, int]
    plan: tuple[ActionStep, ...]
    expansion: tuple[ExpansionRecord, ...]
    root_order: tuple[tuple[int, int], ...]
    root_ordered: bool
    failures: tuple[FailureRecord, ...]
    uncertain: tuple[FailureRecord, ...]
    steps: tuple[KeyStep, ...]
    duration_ms: float
    truncated_failures: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "status": self.status,
            "plan": [a.to_dict() for a in self.plan],
            "expansion_tree": [e.to_dict() for e in self.expansion],
            "root_order": [list(p) for p in self.root_order],
            "root_ordered": self.root_ordered,
            "failures": [f.to_dict() for f in self.failures],
            "uncertain": [f.to_dict() for f in self.uncertain],
            "key_steps": [s.to_dict() for s in self.steps],
            "counters": dict(self.counters),
            "bounds": dict(self.bounds),
            "duration_ms": self.duration_ms,
            "truncated_failures": self.truncated_failures,
            "meta": {
                "domain_name": self.domain_name,
                "problem_name": self.problem_name,
                "engine_version": self.engine_version,
                "domain_version": self.domain_version,
            },
        }
