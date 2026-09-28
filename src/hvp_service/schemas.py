"""Pydantic request/response models for the HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class NodeSpec(BaseModel):
    op: str
    inputs: list[int] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)


class CreateGraphRequest(BaseModel):
    nodes: list[NodeSpec]
    output: int
    description: str | None = None


class CreateGraphResponse(BaseModel):
    graph_id: str
    node_count: int
    output: int
    layout: dict[str, Any]


class GraphInfoResponse(BaseModel):
    graph_id: str
    node_count: int
    output: int
    layout: dict[str, Any]
    point_version: int
    has_stored_point: bool


class SetPointRequest(BaseModel):
    point: list[float]


class SetPointResponse(BaseModel):
    graph_id: str
    point_version: int


class HvpRequest(BaseModel):
    vector: list[float]
    point: list[float] | None = None
    use_stored_point: bool = False
    expected_version: int | None = None
    nonsmooth_policy: Literal["reject", "subgradient"] = "reject"
    subgradient: float = 0.0
    kink_atol: float = 0.0
    max_eval_nodes: int | None = None
    time_budget_ms: float | None = None


class HvpResponse(BaseModel):
    run_id: str
    graph_id: str
    value: float
    gradient: list[float]
    hvp: list[float]
    layout: dict[str, Any]
    diagnostics: dict[str, Any]


class ErrorBody(BaseModel):
    category: str
    message: str
    details: dict[str, Any]
    run_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
