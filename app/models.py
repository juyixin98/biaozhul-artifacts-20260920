"""Request/response protocol models (Pydantic v2).

The JSON protocol uses camelCase; Python code may use snake_case.
Unknown fields are rejected so that typos do not silently disable constraints.
"""

from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Quantity = Union[int, float, str]

TaintEffect = Literal["NoSchedule", "PreferNoSchedule", "NoExecute"]
Operator = Literal["In", "NotIn", "Exists", "DoesNotExist"]
TolerationOperator = Literal["Equal", "Exists"]
UnsatisfiableAction = Literal["DoNotSchedule", "ScheduleAnyway"]


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=lambda name: _to_camel(name),
        populate_by_name=True,
        extra="forbid",
    )


def _to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


# --------------------------------------------------------------------------- #
# Label selectors / affinity
# --------------------------------------------------------------------------- #


class LabelSelectorRequirement(CamelModel):
    key: str
    operator: Operator
    values: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_values(self) -> "LabelSelectorRequirement":
        if self.operator in ("In", "NotIn") and not self.values:
            raise ValueError(f"{self.operator} selector requires at least one value")
        if self.operator in ("Exists", "DoesNotExist") and self.values:
            raise ValueError(f"{self.operator} selector must not carry values")
        return self


class LabelSelector(CamelModel):
    match_labels: dict[str, str] = Field(default_factory=dict)
    match_expressions: list[LabelSelectorRequirement] = Field(default_factory=list)


class NodeSelectorTerm(CamelModel):
    match_expressions: list[LabelSelectorRequirement]

    @field_validator("match_expressions")
    @classmethod
    def _non_empty(cls, v: list[LabelSelectorRequirement]) -> list[LabelSelectorRequirement]:
        if not v:
            raise ValueError("node selector term must contain at least one match expression")
        return v


class PreferredSchedulingTerm(CamelModel):
    weight: int = Field(ge=1, le=100)
    preference: NodeSelectorTerm


class NodeAffinity(CamelModel):
    # OR of terms; each term is an AND of requirements.
    required_during_scheduling_ignored_during_execution: list[NodeSelectorTerm] = Field(
        default_factory=list
    )
    preferred_during_scheduling_ignored_during_execution: list[PreferredSchedulingTerm] = Field(
        default_factory=list
    )


class PodAffinityTerm(CamelModel):
    topology_key: str
    label_selector: Optional[LabelSelector] = None
    # None / omitted means "same namespace as the candidate pod".
    namespaces: Optional[list[str]] = None


class WeightedPodAffinityTerm(CamelModel):
    weight: int = Field(ge=1, le=100)
    pod_affinity_term: PodAffinityTerm


class PodAffinity(CamelModel):
    required_during_scheduling_ignored_during_execution: list[PodAffinityTerm] = Field(
        default_factory=list
    )
    preferred_during_scheduling_ignored_during_execution: list[WeightedPodAffinityTerm] = Field(
        default_factory=list
    )


class PodAntiAffinity(CamelModel):
    required_during_scheduling_ignored_during_execution: list[PodAffinityTerm] = Field(
        default_factory=list
    )
    preferred_during_scheduling_ignored_during_execution: list[WeightedPodAffinityTerm] = Field(
        default_factory=list
    )


class Affinity(CamelModel):
    node_affinity: Optional[NodeAffinity] = None
    pod_affinity: Optional[PodAffinity] = None
    pod_anti_affinity: Optional[PodAntiAffinity] = None


class TopologySpreadConstraint(CamelModel):
    max_skew: int = Field(ge=1)
    topology_key: str
    when_unsatisfiable: UnsatisfiableAction
    # None means the candidate pod's own labels (Kubernetes default).
    label_selector: Optional[LabelSelector] = None


# --------------------------------------------------------------------------- #
# Taints / tolerations / workloads / nodes
# --------------------------------------------------------------------------- #


class Taint(CamelModel):
    key: str
    value: str = ""
    effect: TaintEffect


class Toleration(CamelModel):
    key: str = ""
    operator: TolerationOperator = "Equal"
    value: str = ""
    # Empty effect tolerates every effect of the matching key.
    effect: str = ""

    @model_validator(mode="after")
    def _check_shape(self) -> "Toleration":
        if self.operator == "Equal" and not self.key:
            raise ValueError("Equal toleration requires a key")
        if self.operator == "Exists" and self.value:
            raise ValueError("Exists toleration must not carry a value")
        if self.effect and self.effect not in ("NoSchedule", "PreferNoSchedule", "NoExecute"):
            raise ValueError(f"unsupported toleration effect {self.effect!r}")
        return self


class ExistingPod(CamelModel):
    name: str
    namespace: str = "default"
    node: str
    labels: dict[str, str] = Field(default_factory=dict)
    requests: dict[str, Quantity] = Field(default_factory=dict)


class CandidatePod(CamelModel):
    name: str
    namespace: str = "default"
    requests: dict[str, Quantity] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    tolerations: list[Toleration] = Field(default_factory=list)
    node_selector: dict[str, str] = Field(default_factory=dict)
    affinity: Optional[Affinity] = None
    topology_spread_constraints: list[TopologySpreadConstraint] = Field(default_factory=list)


class Node(CamelModel):
    name: str
    labels: dict[str, str] = Field(default_factory=dict)
    capacity: dict[str, Quantity]
    # When omitted, capacity is used (explicit subset: no system reservation).
    allocatable: Optional[dict[str, Quantity]] = None
    taints: list[Taint] = Field(default_factory=list)


class ScoringWeights(CamelModel):
    least_allocated: float = Field(default=1.0, ge=0)
    preferred_node_affinity: float = Field(default=1.0, ge=0)
    preferred_pod_affinity: float = Field(default=1.0, ge=0)
    taint_preference: float = Field(default=1.0, ge=0)
    topology_spread: float = Field(default=1.0, ge=0)


class AnalyzeRequest(CamelModel):
    nodes: list[Node] = Field(min_length=1)
    existing_pods: list[ExistingPod] = Field(default_factory=list)
    pod: CandidatePod
    scoring_weights: Optional[ScoringWeights] = None

    @model_validator(mode="after")
    def _check_pod_nodes(self) -> "AnalyzeRequest":
        node_names = [n.name for n in self.nodes]
        if len(set(node_names)) != len(node_names):
            raise ValueError("node names must be unique")
        known = set(node_names)
        for p in self.existing_pods:
            if p.node not in known:
                raise ValueError(f"existing pod {p.name!r} references unknown node {p.node!r}")
        return self
