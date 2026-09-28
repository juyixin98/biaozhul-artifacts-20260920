"""Pydantic schemas for the HTTP API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.solver.models import CSPModel


class SolveRequest(BaseModel):
    model: CSPModel
    max_nodes: int = Field(default=100_000, ge=1, le=10_000_000)
    max_backtracks: int = Field(default=100_000, ge=1, le=10_000_000)
    collect_reasons: bool = True


class SolveResponse(BaseModel):
    run_id: str
    status: str  # "sat" | "unsat" | "unknown"
    solution: dict[str, int] | None
    domains: dict[str, list[int]]
    stats: dict[str, Any]
    failure: dict[str, Any] | None
    reasons: list[dict[str, Any]]
    solver_version: str


class RunSummary(BaseModel):
    run_id: str
    created_at: str
    model_name: str
    status: str


class RunDetail(BaseModel):
    run_id: str
    created_at: str
    model_name: str
    model: dict[str, Any]
    status: str
    solution: dict[str, int] | None
    stats: dict[str, Any]
    failure: dict[str, Any] | None
    solver_version: str
    reasons: list[dict[str, Any]]


class FixtureListResponse(BaseModel):
    fixtures: list[str]


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
