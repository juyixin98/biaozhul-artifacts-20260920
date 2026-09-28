"""HTTP request/response schemas (Pydantic v2).

The request body is accepted as a free-form problem dict and parsed by the
domain layer (which gives precise validation errors), while budgets are
explicitly typed query/body fields.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SolveRequest(BaseModel):
    problem: dict[str, Any] = Field(
        ..., description="Temporal planning problem; see docs/SEMANTICS.md"
    )
    node_budget: int | None = Field(default=None, ge=1, le=10_000_000)
    time_budget_seconds: float | None = Field(default=None, gt=0, le=600)


class ReplayRequest(BaseModel):
    problem: dict[str, Any]
    schedule: list[ScheduledItem]


class ScheduledItem(BaseModel):
    action_id: str
    start: int
    end: int


class TimelineItem(BaseModel):
    time: int
    order: int
    kind: str
    action_id: str
    state: dict[str, str]
    resources: dict[str, int]
    note: str


class RunSummary(BaseModel):
    run_id: str
    created_at: str
    status: str
    failure_code: str | None
    best_cost: int | None
    goal_time: int | None
    nodes_expanded: int
    message: str


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    python: str
