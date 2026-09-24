"""Pydantic request envelopes (boundary validation only).

Policy *semantics* are validated by :mod:`app.model` /
:mod:`app.interpreter`; these models enforce envelope shape and forbid
unknown top-level fields.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvaluateRequest(StrictModel):
    policy: dict[str, Any] | None = None
    signed: dict[str, Any] | None = None
    subject: dict[str, Any] | None = None
    resource: dict[str, Any] | None = None


class SetEvaluateRequest(StrictModel):
    policies: list[dict[str, Any]] | None = None
    signed: dict[str, Any] | None = None
    subject: dict[str, Any] | None = None
    resource: dict[str, Any] | None = None


class TruthTableRequest(StrictModel):
    policy: dict[str, Any] | None = None
    policies: list[dict[str, Any]] | None = None
    signed: dict[str, Any] | None = None
    variables: list[dict[str, Any]] = Field(min_length=1)
    mode: str = "ternary"
    include_missing: bool = False
