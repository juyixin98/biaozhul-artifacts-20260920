from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, field_validator


class PurposeCreate(BaseModel):
    key: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)


class PurposeOut(BaseModel):
    id: int
    key: str
    description: str
    created_at: datetime


class PolicyPublish(BaseModel):
    body: str = Field(min_length=1, max_length=100_000)


class PolicyVersionOut(BaseModel):
    policy_id: int
    version: int
    body: str
    published_at: datetime


class _WriteBase(BaseModel):
    event_id: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=0)
    subject_ref: str = Field(min_length=1, max_length=200)


class GrantRequest(_WriteBase):
    action: Literal["grant"] = "grant"
    purpose_key: str = Field(min_length=1, max_length=100)
    policy_version: int = Field(ge=1)
    # Null means the grant never expires.
    expires_at: datetime | None = None
    # Optional client event time; server time is authoritative for sequencing.
    occurred_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def _tz_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        return v


class WithdrawalRequest(_WriteBase):
    action: Literal["withdrawal"] = "withdrawal"
    purpose_key: str = Field(min_length=1, max_length=100)
    occurred_at: datetime | None = None


class ConsentView(BaseModel):
    valid: bool
    status: Literal["granted", "withdrawn", "no_record"]
    reason: str
    organization_id: int
    subject_ref: str
    purpose_key: str
    state_version: int | None
    grant: dict | None = None
    withdrawal: dict | None = None
    basis: dict | None = None


class BatchImportRequest(BaseModel):
    events: list[
        Annotated[
            Union[GrantRequest, WithdrawalRequest],
            Field(discriminator="action"),
        ]
    ] = Field(min_length=1, max_length=500)


class BatchImportResult(BaseModel):
    imported: int
    results: list[dict]


class RebuildResult(BaseModel):
    materialized: int
    replayed_events: int
    took_ms: int


class SubjectDeleteResult(BaseModel):
    deleted_subjects: int
    deleted_states: int
    retained_history_events: int
    retained_audit_logs: int


class ExportCopyCreate(BaseModel):
    copy_label: str = Field(min_length=1, max_length=200)


class AuditOut(BaseModel):
    id: int
    organization_id: int
    actor_role: str
    action: str
    detail: dict
    occurred_at: datetime
