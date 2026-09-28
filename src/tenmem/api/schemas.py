"""Pydantic request/response contracts for the HTTP API.

Two run entry modes:
* ``case``: build one of the bundled synthetic fixtures server-side (name +
  params), so a client can drive every required scenario without sending data.
* ``graph``: submit a full graph dict plus numeric feeds as nested JSON arrays.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PlanRequest(BaseModel):
    graph: dict[str, Any]
    alignment: int = Field(default=64, ge=1)
    max_bytes: int | None = Field(default=None, ge=1)


class RunRequest(BaseModel):
    case: str | None = None
    case_params: dict[str, Any] = Field(default_factory=dict)
    graph: dict[str, Any] | None = None
    feeds: dict[str, list[Any]] | None = None
    constants: dict[str, list[Any]] | None = None
    parallel: bool = False
    alignment: int = Field(default=64, ge=1)
    max_bytes: int | None = Field(default=None, ge=1)
    run_id: str | None = None


class ReleaseRequest(BaseModel):
    output: str


class ErrorBody(BaseModel):
    success: bool = False
    error: dict[str, Any]


def envelope(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data, "error": None}
