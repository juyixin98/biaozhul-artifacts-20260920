"""Domain models for the finite HTN planner.

These Pydantic models are the *internal typed representation* produced by the
rule language parser and consumed by the planner kernel.  They are deliberately
framework independent (no FastAPI imports) so the kernel can be unit tested
without the web layer.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Rule language surface
# ---------------------------------------------------------------------------
class Condition(BaseModel):
    """A single state-literal condition in the rule language.

    kind == "fact"  -> literal ``(name, *args)`` must hold
    kind == "not"   -> literal must be absent (negation as failure)
    kind == "avail" -> resource ``args[0]`` must have >= ``amount`` free units
    kind == "bound" -> resource must have < ``amount`` free units
                       (i.e. at least (capacity - amount + 1) already held)
    kind == "bind"  -> deterministic lookup: exactly one fact
                       ``(name, *args-pattern, value)`` must exist; its last
                       term is bound to ``bind_target``.  Used to derive
                       auxiliary variables (e.g. the unique child of a part).
    """

    kind: Literal["fact", "not", "avail", "bound", "bind"] = "fact"
    name: str
    args: list[str] = Field(default_factory=list)
    amount: int = 1
    bind_target: str | None = None

    def literal_key(self) -> tuple[str, ...]:
        return (self.name, *self.args)


class Effect(BaseModel):
    """State transition of a primitive action.

    add/remove are ground facts.  reserve/release name a shared resource and a
    unit count.  reserve moves units from free -> held; release moves them back.
    """

    add: list[list[str]] = Field(default_factory=list)
    remove: list[list[str]] = Field(default_factory=list)
    reserve: list[dict] = Field(default_factory=list)
    release: list[list] = Field(default_factory=list)


class Primitive(BaseModel):
    """A primitive (leaf) action: directly executable against the state."""

    name: str
    parameters: list[str] = Field(default_factory=list)
    precondition: list[Condition] = Field(default_factory=list)
    effect: Effect = Field(default_factory=Effect)
    # Parsed and retained; the planner returns an executable ordering rather
    # than a shortest-makespan one (see README "Deliberate non-goals").
    duration: int = 1

    @field_validator("duration")
    @classmethod
    def _duration_positive(cls, v: int) -> int:
        if v < 0:
            raise ValueError("duration must be >= 0")
        return v


class Subtask(BaseModel):
    """A child invocation inside a method body.

    order:
      - "sequential": ordered list; ``after`` is ignored.
      - "partial":    partial order; ``after`` lists sibling ids that must run
                      first.  Dependencies always point to earlier-declared
                      siblings (validated at parse time) so the sibling DAG is
                      acyclic by construction.
    """

    id: str
    task: str
    args: list[str] = Field(default_factory=list)
    after: list[str] = Field(default_factory=list)


class Method(BaseModel):
    """A decomposition method for a compound task.

    ``priority`` orders method attempts (lower first).  ``guard`` conditions are
    evaluated against the *current* state before expansion, which is what makes
    two methods on the same task mutually exclusive under a given state.
    """

    name: str
    task: str
    parameters: list[str] = Field(default_factory=list)
    guard: list[Condition] = Field(default_factory=list)
    subtasks: list[Subtask]
    order: Literal["sequential", "partial"] = "sequential"

    @field_validator("subtasks")
    @classmethod
    def _validate_subtasks(cls, v: list[Subtask]) -> list[Subtask]:
        if not v:
            raise ValueError("method must declare at least one subtask")
        ids: set[str] = set()
        position: dict[str, int] = {}
        for i, st in enumerate(v):
            if st.id in ids:
                raise ValueError(f"duplicate subtask id: {st.id!r}")
            ids.add(st.id)
            position[st.id] = i
        for st in v:
            for dep in st.after:
                if dep not in ids:
                    raise ValueError(
                        f"subtask {st.id!r} depends on unknown sibling {dep!r}"
                    )
                if position[dep] >= position[st.id]:
                    raise ValueError(
                        f"subtask {st.id!r} may only depend on earlier "
                        f"siblings; {dep!r} is declared later (forward refs "
                        "are rejected to keep the sibling DAG acyclic)"
                    )
        return v


class Resource(BaseModel):
    """A shared, finite, replenishable resource."""

    name: str
    capacity: int

    @field_validator("capacity")
    @classmethod
    def _capacity_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("resource capacity must be > 0")
        return v


class Domain(BaseModel):
    """A complete planning domain."""

    name: str
    version: str = "1.0.0"
    primitives: dict[str, Primitive]
    methods: list[Method]
    resources: dict[str, Resource] = Field(default_factory=dict)

    def methods_for(self, task_name: str) -> list[Method]:
        """Methods for a task sorted by ascending priority-agnostic name.

        Selection order is deterministic (priority then name) and computed in
        the kernel; here we simply group.
        """
        return [m for m in self.methods if m.task == task_name]

    @field_validator("primitives")
    @classmethod
    def _primitives_nonempty(cls, v: dict[str, Primitive]) -> dict[str, Primitive]:
        if not v:
            raise ValueError("domain must define at least one primitive")
        return v


class Problem(BaseModel):
    """A concrete planning problem: an initial state and a goal task."""

    name: str
    domain: str
    initial_facts: list[list[str]] = Field(default_factory=list)
    initial_resources: dict[str, int] = Field(default_factory=dict)
    goal_task: str
    goal_args: list[str] = Field(default_factory=list)
    max_depth: int = 8
    max_expansions: int = 256


# ---------------------------------------------------------------------------
# Planner outputs / evidence
# ---------------------------------------------------------------------------
class FailureKind(str, Enum):
    UNRESOLVABLE_TASK = "unresolvable_task"
    GUARD_REJECTED = "guard_rejected"
    PRECONDITION_NOT_STAT = "precondition_not_stat"
    RESOURCE_UNAVAILABLE = "resource_unavailable"
    DEPTH_EXCEEDED = "depth_exceeded"
    EXPANSION_BUDGET_EXHAUSTED = "expansion_budget_exhausted"
    METHOD_CYCLE = "method_cycle"
    PARTIAL_ORDER_INFEASIBLE = "partial_order_infeasible"
    NO_VIABLE_METHOD = "no_viable_method"


class FailureEvidence(BaseModel):
    kind: FailureKind
    task: str | None = None
    method: str | None = None
    detail: str
    depth: int | None = None
    tried_methods: list[str] = Field(default_factory=list)
    rejected_guards: list[dict] = Field(default_factory=list)
    node_path: list[str] = Field(default_factory=list)


class Node(BaseModel):
    """One node of the retained expansion tree.

    kind == "compound": expanded by ``method`` into children (abstract task).
    kind == "primitive": a leaf bound to ``primitive``; executable and, when the
    whole plan is feasible, ordered topologically among siblings.
    """

    node_id: str
    kind: Literal["compound", "primitive"]
    task: str
    args: list[str] = Field(default_factory=list)
    method: str | None = None
    primitive: str | None = None
    children: list[str] = Field(default_factory=list)
    after: list[str] = Field(default_factory=list)
    depth: int
    status: Literal["expanded", "executable", "failed"] = "expanded"
    detail: str | None = None
    guard_conditions: list[dict] | None = None
    guard_snapshot: list[list[str]] | None = None


class PlanResult(BaseModel):
    feasible: bool
    request_id: str | None = None
    domain: str
    domain_version: str
    problem: str
    goal_task: str
    goal_args: list[str] = Field(default_factory=list)
    execution_order: list[str] = Field(default_factory=list)
    nodes: dict[str, Node] = Field(default_factory=dict)
    roots: list[str] = Field(default_factory=list)
    # Terminal evidence when infeasible.
    failures: list[FailureEvidence] = Field(default_factory=list)
    # Losing branches explored by backtracking on an otherwise feasible plan.
    abandoned_branches: list[FailureEvidence] = Field(default_factory=list)
    depth_used: int = 0
    expansions_used: int = 0
    # Conclusions that could not be decided; listed separately, never as passes.
    uncertainty: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def node_label(self, node_id: str) -> str:
        n = self.nodes[node_id]
        target = n.primitive if n.kind == "primitive" else n.task
        return f"{node_id}:{target}({', '.join(n.args)})"
