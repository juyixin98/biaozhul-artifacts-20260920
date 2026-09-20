"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Role = Literal["admin", "auditor"]
Action = Literal["grant", "withdraw"]


class PublishPolicyIn(BaseModel):
    body: str = Field(min_length=1, max_length=100_000)


class PolicyVersionOut(BaseModel):
    version: int
    body: str
    published_at: datetime


class GrantIn(BaseModel):
    """A single consent write (grant or withdrawal).

    ``expected_version`` is an optimistic-concurrency token: pass the state
    version the client last saw (0 when no state exists yet). A stale value
    yields HTTP 409 instead of applying.
    """

    event_id: str = Field(min_length=1, max_length=100)
    subject_key: str = Field(min_length=1, max_length=400)
    purpose: str = Field(min_length=1, max_length=200)
    action: Action
    expected_version: int = Field(ge=0)
    expires_at: datetime | None = None
    # Only meaningful for grants; when null the organization's latest policy is
    # pinned. May be supplied explicitly to grant under an older valid version.
    policy_version: int | None = None

    @field_validator("expires_at")
    @classmethod
    def _tz_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        return v


class WriteResultOut(BaseModel):
    event_id: str
    replayed: bool
    state_version: int
    status: Literal["granted", "withdrawn"]
    policy_version: int | None
    expires_at: datetime | None
    created_at: datetime


class ConsentOut(BaseModel):
    subject_key: str
    purpose: str
    valid: bool
    status: Literal["granted", "withdrawn", "no_record"]
    reason: str
    state_version: int | None
    basis_event_id: str | None
    policy_version: int | None
    expires_at: datetime | None
    evaluated_at: datetime


class EventOut(BaseModel):
    event_id: str
    subject_key: str | None
    purpose: str
    action: Action
    policy_version: int | None
    expires_at: datetime | None
    expected_version: int
    state_version: int
    created_at: datetime


class ImportIn(BaseModel):
    events: list[GrantIn] = Field(min_length=1, max_length=500)


class ImportOut(BaseModel):
    accepted: int
    replayed: int
    results: list[WriteResultOut]


class ExportCreateIn(BaseModel):
    label: str = Field(default="dsar-export", min_length=1, max_length=200)
    payload: dict


class ExportOut(BaseModel):
    id: int
    label: str
    payload: dict
    created_at: datetime


class EraseOut(BaseModel):
    subject_key: str
    erased: bool
    already_erased: bool
    deleted_at: datetime


class RebuildOut(BaseModel):
    replayed_events: int
    materialized_states: int
    skipped_tombstoned_subjects: int


class AuditOut(BaseModel):
    id: int
    action: str
    outcome: Literal["success", "failure"]
    subject_id: int | None
    event_id: str | None
    policy_version: int | None
    detail: dict | None
    created_at: datetime
