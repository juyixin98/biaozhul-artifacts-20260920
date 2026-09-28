"""Pydantic request/response models for the HTTP boundary."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class OutputItem(BaseModel):
    rank: int
    output: str
    cost: float
    lex_rank: int = Field(
        description="Lexicographic rank among outputs sharing this cost"
    )


class QueryRequest(BaseModel):
    corpus_id: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=128)
    input: str = Field(min_length=1, max_length=128, description="Query string")
    k: int = Field(default=5, gt=0, le=100)
    budget: int = Field(default=100_000, gt=0)

    model_config = {"extra": "forbid"}


class QueryResponse(BaseModel):
    run_id: str
    corpus_id: str
    target: str
    input: str
    version: str
    status: Literal["ok", "no_path", "budget_exhausted", "error"]
    complete: bool
    accepted: bool
    k: int
    budget: int
    expansions: int
    budget_used_fraction: float
    outputs: list[OutputItem]
    decision: str
    logs: list[str] = Field(
        description="Run-correlated computation trace (same run_id)"
    )


class ErrorResponse(BaseModel):
    error_code: str
    message: str
    run_id: str | None = None
    details: dict | None = None


class ModelSummary(BaseModel):
    name: str
    kind: str
    num_states: int
    num_arcs: int


class CorpusSummary(BaseModel):
    corpus_id: str
    description: str
    loaded_at: str
    transducers: list[ModelSummary]
    pipelines: dict[str, list[str]]


class LoadReportResponse(BaseModel):
    corpus_id: str
    models: list[ModelSummary]
    pipelines: list[str]
    logs: list[str]
