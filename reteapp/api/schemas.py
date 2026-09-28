"""Pydantic request/response schemas for the HTTP boundary."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CreateRunRequest(BaseModel):
    rules: list[dict[str, Any]] | dict[str, Any] = Field(
        ..., description="Rule document: list of rules or {'rules': [...]}"
    )
    run_id: str | None = Field(default=None, description="Optional explicit correlation id")
    duplicate_policy: Literal["multiset", "ignore", "reject"] = "multiset"


class InsertFactRequest(BaseModel):
    fact: dict[str, Any] = Field(
        ..., description="{'type': str, 'fields': {str: json-primitive}}"
    )
    duplicate_policy: Literal["multiset", "ignore", "reject"] | None = None


class FireRequest(BaseModel):
    mode: Literal["next", "step", "all"]
    max_firings: int | None = Field(default=None, ge=1, le=10_000)
