"""Pydantic request/response contracts for the HTTP API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class VariableSpec(BaseModel):
    name: str = Field(..., min_length=1)
    shape: list[int] = Field(default_factory=list)


class NodeSpec(BaseModel):
    id: str = Field(..., min_length=1)
    op: str = Field(..., min_length=1)
    args: list[str] = Field(default_factory=list)
    index: int | None = None
    value: float | None = None


class ExpressionSpec(BaseModel):
    nodes: list[NodeSpec]
    output: str = Field(..., min_length=1)


class NonsmoothSpec(BaseModel):
    policy: str = "reject"
    subgradient: float = 0.0


class BudgetSpec(BaseModel):
    max_nodes: int = 200_000
    max_evals: int = 2_000_000


class CreateFunctionRequest(BaseModel):
    variables: list[VariableSpec]
    expression: ExpressionSpec
    nonsmooth: NonsmoothSpec = Field(default_factory=NonsmoothSpec)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)


class SetPointRequest(BaseModel):
    point: dict[str, Any]
    expected_version: int | None = None


class HvpRequest(BaseModel):
    vector: dict[str, Any]


class VerifyRequest(BaseModel):
    vector: dict[str, Any] | None = None  # default: deterministic probes + zero
    tolerance: float = 1e-8


class ErrorBody(BaseModel):
    category: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = None
