"""Pydantic request / response schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------- #
# Subjects / exports / erasure
# --------------------------------------------------------------------------- #


class SubjectCreate(BaseModel):
    external_ref: str = Field(min_length=1, max_length=300)
    email: str | None = Field(default=None, max_length=320)
    display_name: str | None = Field(default=None, max_length=300)


class SubjectOut(BaseModel):
    id: int
    external_ref: str | None
    email: str | None
    display_name: str | None
    erased: bool
    created_at: datetime
    erased_at: datetime | None

    model_config = {"from_attributes": True}


class ExportCreate(BaseModel):
    subject_id: int
    destination: str = Field(min_length=1, max_length=300)
    payload: str = Field(min_length=1)


class ExportOut(BaseModel):
    id: int
    subject_id: int
    destination: str
    created_at: datetime

    model_config = {"from_attributes": True}


# --------------------------------------------------------------------------- #
# Policies
# --------------------------------------------------------------------------- #


class PolicyPublish(BaseModel):
    version: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1)


class PolicyOut(BaseModel):
    id: int
    version: str
    body: str
    published_at: datetime

    model_config = {"from_attributes": True}


# --------------------------------------------------------------------------- #
# Consent events
# --------------------------------------------------------------------------- #


class GrantRequest(BaseModel):
    event_id: str = Field(min_length=1, max_length=100)
    subject_id: int
    purpose: str = Field(min_length=1, max_length=200)
    # Stream version the client believes it is appending after. Use 0 for a
    # brand-new stream.
    expected_version: int = Field(ge=0)
    policy_version: str = Field(min_length=1, max_length=100)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def _aware_utc(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware (use UTC)")
        return v


class WithdrawRequest(BaseModel):
    event_id: str = Field(min_length=1, max_length=100)
    subject_id: int
    purpose: str = Field(min_length=1, max_length=200)
    expected_version: int = Field(ge=0)


class ConsentEventOut(BaseModel):
    event_id: str
    subject_id: int
    purpose: str
    event_type: str
    version: int
    policy_version: str | None
    expires_at: datetime | None
    created_at: datetime
    replayed: bool = False


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #


class ConsentVerification(BaseModel):
    subject_id: int
    purpose: str
    valid: bool
    reason: str
    current_version: int
    grant_event_id: str | None
    withdraw_event_id: str | None
    policy_version: str | None
    expires_at: datetime | None
    evaluated_at: datetime


# --------------------------------------------------------------------------- #
# Batch import
# --------------------------------------------------------------------------- #


class BatchItem(BaseModel):
    event_id: str = Field(min_length=1, max_length=100)
    subject_id: int
    purpose: str = Field(min_length=1, max_length=200)
    expected_version: int = Field(ge=0)
    policy_version: str | None = None
    expires_at: datetime | None = None
    event_type: str = Field(pattern="^(grant|withdraw)$")

    @field_validator("expires_at")
    @classmethod
    def _aware_utc(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware (use UTC)")
        return v


class BatchImport(BaseModel):
    items: list[BatchItem] = Field(min_length=1, max_length=500)


class BatchResultItem(BaseModel):
    event_id: str
    version: int
    event_type: str
    replayed: bool


class BatchResult(BaseModel):
    imported: int
    replayed: int
    items: list[BatchResultItem]


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


class AuditOut(BaseModel):
    id: int
    action: str
    detail: dict | None
    created_at: datetime

    model_config = {"from_attributes": True}
