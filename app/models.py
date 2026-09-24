"""请求/响应数据模型（Pydantic v2）。

只建模一个明确子集：
- 节点：可分配资源（cpu/memory）、标签、污点
- 已有 Pod：所在节点、资源 request、标签
- 待调度 Pod：资源 request、nodeSelector、节点亲和（required/preferred）、
  Pod 反亲和（required/preferred）、容忍度、拓扑分布（软约束）
资源一律按 request 计算，不使用任何实时利用率。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .quantity import QuantityParseError, cpu_to_milli, memory_to_bytes


class ResourceList(BaseModel):
    """cpu 用 K8s 资源量字符串（如 "500m"、"2"），memory 同理（如 "128Mi"、"1Gi"）。"""

    cpu: str = "0"
    memory: str = "0"

    @field_validator("cpu")
    @classmethod
    def _check_cpu(cls, v: str) -> str:
        cpu_to_milli(v)  # 解析失败会抛 QuantityParseError（ValueError 子类）
        return v

    @field_validator("memory")
    @classmethod
    def _check_memory(cls, v: str) -> str:
        memory_to_bytes(v)
        return v

    def cpu_milli(self) -> int:
        return cpu_to_milli(self.cpu)

    def memory_bytes(self) -> int:
        return memory_to_bytes(self.memory)


class Taint(BaseModel):
    key: str
    value: str | None = None
    effect: Literal["NoSchedule", "PreferNoSchedule", "NoExecute"]


class Toleration(BaseModel):
    key: str
    operator: Literal["Equal", "Exists"] = "Equal"
    value: str | None = None
    effect: Literal["NoSchedule", "PreferNoSchedule", "NoExecute"] | None = None

    def tolerates(self, taint: Taint) -> bool:
        if self.effect is not None and self.effect != taint.effect:
            return False
        if self.key != taint.key:
            return False
        if self.operator == "Exists":
            return True
        return self.value == taint.value


class NodeSelectorRequirement(BaseModel):
    key: str
    operator: Literal["In", "NotIn", "Exists", "DoesNotExist", "Gt", "Lt"]
    values: list[str] = Field(default_factory=list)


class NodeSelectorTerm(BaseModel):
    """term 内 matchExpressions 为 AND，term 之间为 OR（K8s 语义）。"""

    match_expressions: list[NodeSelectorRequirement] = Field(default_factory=list)


class PreferredNodeAffinityTerm(BaseModel):
    weight: int = Field(ge=1, le=100)
    term: NodeSelectorTerm


class NodeAffinity(BaseModel):
    required_terms: list[NodeSelectorTerm] = Field(default_factory=list)
    preferred_terms: list[PreferredNodeAffinityTerm] = Field(default_factory=list)


class PodAffinityTerm(BaseModel):
    """反亲和项：matchLabels 命中的 Pod 出现在同一 topologyKey 域即冲突。"""

    match_labels: dict[str, str] = Field(default_factory=dict)
    topology_key: str = "kubernetes.io/hostname"


class PreferredPodAntiAffinityTerm(BaseModel):
    weight: int = Field(ge=1, le=100)
    term: PodAffinityTerm


class PodAntiAffinity(BaseModel):
    required_terms: list[PodAffinityTerm] = Field(default_factory=list)
    preferred_terms: list[PreferredPodAntiAffinityTerm] = Field(default_factory=list)


class TopologySpreadRule(BaseModel):
    """软约束：希望 match_labels 命中的 Pod 在 topology_key 各取值间均匀分布。"""

    topology_key: str = "topology.kubernetes.io/zone"
    match_labels: dict[str, str] = Field(default_factory=dict)


class PodSpec(BaseModel):
    name: str
    labels: dict[str, str] = Field(default_factory=dict)
    requests: ResourceList = Field(default_factory=ResourceList)
    node_selector: dict[str, str] = Field(default_factory=dict)
    tolerations: list[Toleration] = Field(default_factory=list)
    node_affinity: NodeAffinity = Field(default_factory=NodeAffinity)
    anti_affinity: PodAntiAffinity = Field(default_factory=PodAntiAffinity)
    topology_spread: TopologySpreadRule | None = None


class NodeSpec(BaseModel):
    name: str
    labels: dict[str, str] = Field(default_factory=dict)
    allocatable: ResourceList
    taints: list[Taint] = Field(default_factory=list)


class ExistingPod(BaseModel):
    name: str
    node_name: str
    labels: dict[str, str] = Field(default_factory=dict)
    requests: ResourceList = Field(default_factory=ResourceList)


class ScoreWeights(BaseModel):
    """各评分插件权重；0 表示禁用该插件。均为软约束，绝不参与硬过滤。"""

    least_allocated: int = Field(default=1, ge=0)
    zone_spread: int = Field(default=1, ge=0)
    preferred_node_affinity: int = Field(default=1, ge=0)
    preferred_anti_affinity: int = Field(default=1, ge=0)
    prefer_no_schedule_taint: int = Field(default=1, ge=0)


class AnalyzeRequest(BaseModel):
    pod: PodSpec
    nodes: list[NodeSpec] = Field(min_length=1)
    existing_pods: list[ExistingPod] = Field(default_factory=list)
    weights: ScoreWeights = Field(default_factory=ScoreWeights)


class NodeScoreBreakdown(BaseModel):
    least_allocated: float
    zone_spread: float
    preferred_node_affinity: float
    preferred_anti_affinity: float
    prefer_no_schedule_taint: float


class NodeResult(BaseModel):
    node: str
    feasible: bool
    filter_reasons: list[str] = Field(default_factory=list)
    scores: NodeScoreBreakdown | None = None
    final_score: float | None = None
    rank: int | None = None


class AnalyzeResponse(BaseModel):
    pod: str
    winner: str | None
    results: list[NodeResult]
