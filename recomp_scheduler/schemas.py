"""HTTP API 的 Pydantic 契约。

这些模型与内部领域模型（graph/planner/engine）解耦：API 只接收可 JSON 化的
声明，服务层负责转换。错误响应用统一信封（见 :class:`ErrorEnvelope`）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class NodeSpec(BaseModel):
    id: str = Field(..., min_length=1, description="节点唯一 id")
    op: str = Field(..., description="算子名: input|linear|relu|tanh|add|mul|dropout")
    inputs: list[str] = Field(default_factory=list)
    attrs: dict[str, Any] = Field(default_factory=dict)


class PlanRequest(BaseModel):
    nodes: list[NodeSpec] = Field(..., min_length=1)
    outputs: list[str] | None = None
    seed: int = 1234
    budget_elements: int = Field(..., gt=0, description="内存预算（float64 元素数）")
    verify_gradients: bool = True
    finite_difference: bool = Field(
        default=False,
        description="是否用独立有限差分做黄金参考（小图，成本随标量数线性）",
    )


class ExhaustiveRequest(BaseModel):
    nodes: list[NodeSpec]
    outputs: list[str] | None = None


class BlockView(BaseModel):
    index: int
    nodes: list[str]


class PlanView(BaseModel):
    boundary_positions: list[int]
    block_count: int
    blocks: list[BlockView]


class ProfileView(BaseModel):
    peak_elements: int
    peak_live_without_workspace: int
    seam_retained: dict[str, int]
    recompute_flops: int
    forward_flops: int
    backward_flops: int
    total_flops: int
    recompute_ratio: float


class CandidateView(BaseModel):
    plan: PlanView
    profile: ProfileView


class ErrorEnvelope(BaseModel):
    success: bool = False
    error_category: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = None


class PlanResponse(BaseModel):
    success: bool = True
    run_id: str
    graph_fingerprint: str
    budget_elements: int
    plan: PlanView
    profile: ProfileView
    runtime: dict[str, Any]
    gradient_check: dict[str, Any] | None = None
    candidates: dict[str, int]
    reason: str


class ExhaustiveResponse(BaseModel):
    success: bool = True
    graph_fingerprint: str
    legal_candidate_count: int
    candidates: list[CandidateView]
    baseline: CandidateView
    min_peak: int
    min_recompute_at_min_peak: int
