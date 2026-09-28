"""Request/response Pydantic models."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class CreateProblemRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(..., min_length=1, max_length=200)
    source: str = Field(..., min_length=1)


class QueryRequest(BaseModel):
    node_id: str = Field(..., min_length=1, max_length=120)
    # Optional fixed context (set of assumptions) for the query.
    environment: Optional[List[str]] = None


class RetractRequest(BaseModel):
    assumptions: List[str] = Field(..., min_length=1)


class PropagateRequest(BaseModel):
    # Per-request budget overrides; null means use configured defaults.
    max_label_envs: Optional[int] = Field(None, ge=1, le=100000)
    max_total_envs: Optional[int] = Field(None, ge=1, le=10_000_000)
    max_steps: Optional[int] = Field(None, ge=1, le=100_000_000)
