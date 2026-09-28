"""HTTP API request/response schemas."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.rules.models import (
    Plan,
    Problem,
    ReplayResult,
    SearchResult,
)


class SolveOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    budget_nodes: int = Field(default=200_000, ge=1)
    max_steps: int = Field(default=32, ge=1, le=256)
    max_occurrences_per_action: int | None = Field(default=None, ge=1)
    cross_check_reference: bool = Field(
        default=False,
        description="additionally run the independent exhaustive reference on the small grid",
    )
    reference_max_steps: int = Field(default=6, ge=0, le=12)
    reference_max_occurrences: int = Field(default=3, ge=0, le=8)


class SolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    problem: Problem
    options: SolveOptions = Field(default_factory=SolveOptions)


class ReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    problem: Problem
    plan: Plan


class ReferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    problem: Problem
    max_steps: int = Field(default=6, ge=0, le=12)
    max_occurrences_per_action: int = Field(default=3, ge=0, le=8)


class ReferenceSummary(BaseModel):
    found: bool
    optimal_makespan: int | None
    schedules_evaluated: int


class RunRef(BaseModel):
    run_id: str
    input_fingerprint: str
    engine_version: str
    created_at: str | None = None


class SolveResponse(BaseModel):
    run: RunRef
    search: SearchResult
    replay: ReplayResult | None = None
    reference: ReferenceSummary | None = None
    cross_check_agrees: bool | None = None
    cross_check_detail: str | None = None


class ReplayResponse(BaseModel):
    run: RunRef
    replay: ReplayResult


class ErrorBody(BaseModel):
    error: dict[str, Any]
