"""Pydantic request/response schemas (kept separate from domain types)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

RNG_STRATEGIES = ("counter", "snapshot")


class NodeSchema(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    op: str
    inputs: List[str] = Field(default_factory=list)
    params: Dict[str, Any] = Field(default_factory=dict)


class ParameterSchema(BaseModel):
    shape: List[int] = Field(..., min_length=1)
    seed: int = Field(..., ge=0, le=2**32 - 1)

    @field_validator("shape")
    @classmethod
    def _positive_dims(cls, v: List[int]) -> List[int]:
        if not v or any(d <= 0 for d in v):
            raise ValueError("shape dimensions must be positive")
        return v


class CustomGraphSchema(BaseModel):
    target: str = Field(..., min_length=1)
    nodes: List[NodeSchema] = Field(..., min_length=1)
    parameters: Dict[str, ParameterSchema]
    inputs: Dict[str, List[Any]]


class CreateSessionRequest(BaseModel):
    fixture: Optional[str] = Field(default=None)
    graph: Optional[CustomGraphSchema] = Field(default=None)
    master_seed: int = Field(default=1234, ge=0, le=2**32 - 1)

    @field_validator("fixture")
    @classmethod
    def _fixture_nonempty(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v:
            raise ValueError("fixture must be a non-empty name")
        return v


class PlanRequest(BaseModel):
    memory_budget: Optional[int] = Field(default=None, gt=0)
    rng_strategy: str = Field(default="counter")
    force_search: Optional[str] = Field(default=None)

    @field_validator("rng_strategy")
    @classmethod
    def _strategy(cls, v: str) -> str:
        if v not in RNG_STRATEGIES:
            raise ValueError(f"rng_strategy must be one of {RNG_STRATEGIES}")
        return v

    @field_validator("force_search")
    @classmethod
    def _search(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("exhaustive", "greedy"):
            raise ValueError("force_search must be 'exhaustive' or 'greedy'")
        return v


class RunRequest(BaseModel):
    master_seed: int = Field(default=1234, ge=0, le=2**32 - 1)
    finite_difference: bool = Field(default=False)
    fd_eps: float = Field(default=1e-6, gt=0, lt=1e-2)


class ApplyRequest(BaseModel):
    lr: float = Field(default=0.1, gt=0, le=10.0)


class GradientSummary(BaseModel):
    parameter: str
    shape: List[int]
    fingerprint: str
    max_abs: float
    l2_norm: float


class VerificationReport(BaseModel):
    oracle_source: str
    loss_abs_diff: float
    grad_max_abs_diff: Dict[str, float]
    finite_difference_max_abs_diff: Optional[Dict[str, float]] = None
    passed: bool
    reasons: List[str] = Field(default_factory=list)
    tolerances: Dict[str, float] = Field(default_factory=dict)


class PlanResponse(BaseModel):
    session_id: str
    run_id: str
    plan: Dict[str, Any]
    feasible: bool
    note: str


class RunResponse(BaseModel):
    session_id: str
    run_id: str
    status: str
    loss: float
    peak_memory: int
    forward_flops: int
    recompute_flops: int
    backward_flops: int
    extra_compute_ratio: float
    recomputed_nodes: List[str]
    replay_waves: List[Dict[str, Any]]
    emitted_side_effects: int
    rng_strategy: str
    rng_replay_ok: bool
    gradients: List[GradientSummary]
    verification: VerificationReport
    training_step: int


class SessionResponse(BaseModel):
    session_id: str
    fixture: Optional[str]
    target: str
    order: List[str]
    shapes: Dict[str, List[int]]
    shared_nodes: List[str]
    training_step: int
    planned: bool
    run_count: int


class ErrorResponse(BaseModel):
    error: Dict[str, Any]
