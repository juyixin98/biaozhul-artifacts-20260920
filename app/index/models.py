"""Stored record models for the index layer."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class RuleSetRecord(BaseModel):
    """A persisted ruleset: its corpus spec plus the computed diagnostics."""

    id: str
    name: str
    created_at: str
    spec: dict[str, Any]
    diagnostics: dict[str, Any]


class RunLogEntry(BaseModel):
    """One structured run-log entry, replayable by run id."""

    run_id: str
    ts: str
    op: str
    stage: str
    detail: dict[str, Any]
