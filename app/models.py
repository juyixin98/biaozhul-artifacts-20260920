"""Domain models for the local cluster snapshot and drain plans.

Field names mirror Kubernetes objects (camelCase) so example snapshots can be
authored like real manifests; internally everything is snake_case.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class KubeModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=False)


# --------------------------------------------------------------------------- #
# Snapshot objects
# --------------------------------------------------------------------------- #
class LabelSelector(KubeModel):
    match_labels: dict[str, str] = Field(default_factory=dict, alias="matchLabels")


class Node(KubeModel):
    name: str
    schedulable: bool = Field(default=True, alias="schedulable")
    labels: dict[str, str] = Field(default_factory=dict)

    @property
    def cordoned(self) -> bool:
        return not self.schedulable


class OwnerReference(KubeModel):
    kind: str = Field(alias="kind")
    name: str = Field(alias="name")
    uid: str = Field(default="", alias="uid")


class Pod(KubeModel):
    name: str
    namespace: str = "default"
    node: str = Field(alias="node")
    phase: Literal["Pending", "Running", "Succeeded", "Failed"] = "Running"
    ready: bool = True
    labels: dict[str, str] = Field(default_factory=dict)
    owner: Optional[OwnerReference] = Field(default=None, alias="owner")
    priority: int = 0
    static_pod: bool = Field(default=False, alias="staticPod")
    mirror: bool = Field(default=False, alias="mirror")
    # Tick on which a Pending/not-ready pod becomes ready (simulator use).
    ready_at: Optional[int] = Field(default=None, alias="readyAt")
    # Set by the simulator once an eviction is admitted.
    deletion_tick: Optional[int] = Field(default=None, alias="deletionTick")
    # Origin: "original", "replacement", "external".
    origin: str = "original"
    uid: str = ""

    @property
    def deleting(self) -> bool:
        return self.deletion_tick is not None

    @property
    def terminal(self) -> bool:
        return self.phase in ("Succeeded", "Failed")


class Deployment(KubeModel):
    name: str
    namespace: str = "default"
    replicas: int
    selector: LabelSelector = Field(default_factory=LabelSelector)
    # Ticks a newly created replacement pod spends Pending + not-ready.
    ready_delay: int = Field(default=2, alias="readyDelay")


class ReplicaSet(KubeModel):
    name: str
    namespace: str = "default"
    replicas: int
    selector: LabelSelector = Field(default_factory=LabelSelector)


class DaemonSet(KubeModel):
    name: str
    namespace: str = "default"
    selector: LabelSelector = Field(default_factory=LabelSelector)


class PodDisruptionBudget(KubeModel):
    name: str
    namespace: str = "default"
    selector: LabelSelector = Field(default_factory=LabelSelector)
    # intstr: absolute integer or "n%"
    min_available: Optional[int | str] = Field(default=None, alias="minAvailable")
    max_unavailable: Optional[int | str] = Field(default=None, alias="maxUnavailable")
    # k8s 1.26+: IfHealthyBudget (default) | AlwaysAllow
    unhealthy_policy: Literal["IfHealthyBudget", "AlwaysAllow"] = Field(
        default="IfHealthyBudget", alias="unhealthyPodEvictionPolicy"
    )

    @model_validator(mode="after")
    def _exactly_one_mode(self) -> "PodDisruptionBudget":
        if (self.min_available is None) == (self.max_unavailable is None):
            raise ValueError(
                f"pdb {self.name}: exactly one of minAvailable / maxUnavailable must be set"
            )
        return self


class Snapshot(KubeModel):
    generation: int = Field(default=0, alias="generation")
    tick: int = 0
    nodes: list[Node] = Field(default_factory=list)
    pods: list[Pod] = Field(default_factory=list)
    deployments: list[Deployment] = Field(default_factory=list)
    replica_sets: list[ReplicaSet] = Field(default_factory=list, alias="replicaSets")
    daemon_sets: list[DaemonSet] = Field(default_factory=list, alias="daemonSets")
    pdbs: list[PodDisruptionBudget] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Planner output
# --------------------------------------------------------------------------- #
class DrainState(str, Enum):
    PLANNED = "PLANNED"          # next step is a CordonStep, not yet started
    WAITING = "WAITING"          # current preconditions not satisfied; step still safe
    READY = "READY"              # current step's preconditions all hold
    COMPLETE = "COMPLETE"
    BLOCKED = "BLOCKED"          # a non-evictable pod forbids draining a node
    CANCELLED = "CANCELLED"


class Precondition(KubeModel):
    id: str
    description: str
    satisfied: bool
    detail: str = ""
    # "hard" => step cannot execute; "soft" => informational/will re-check.
    severity: Literal["hard", "soft"] = "hard"


class CordonStep(KubeModel):
    kind: Literal["Cordon"] = "Cordon"
    node: str


class EvictWaveStep(KubeModel):
    kind: Literal["EvictWave"] = "EvictWave"
    index: int
    pods: list[str]
    # Planner-time prediction of the preconditions (re-evaluated at run time).
    predicted_pdbs: dict[str, int] = Field(
        default_factory=dict,
        alias="predictedPdbs",
        description="pdb key -> disruptionsRemaining after previous waves settled",
    )


class CompleteStep(KubeModel):
    kind: Literal["Complete"] = "Complete"
    nodes: list[str]
    uncordon: bool = False


Step = CordonStep | EvictWaveStep | CompleteStep


class Blocker(KubeModel):
    node: str
    pod: str
    namespace: str
    reason: str


class Plan(KubeModel):
    drain_id: str = Field(alias="drainId")
    generation: int
    nodes: list[str]
    ignore_daemon_sets: bool = Field(default=True, alias="ignoreDaemonSets")
    steps: list[Step]
    blockers: list[Blocker] = Field(default_factory=list)
    # pdb key -> healthy count captured when the plan was made (audit baseline)
    pdb_baseline: dict[str, int] = Field(default_factory=dict, alias="pdbBaseline")
    rationale: list[str] = Field(default_factory=list)


class Event(KubeModel):
    tick: int
    generation: int
    type: str
    detail: str
