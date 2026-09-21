from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .models import AlertRule, AlertStatus, BaselineStatus, EventType, UserRole


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: UserRole
    username: str


class EventIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    device_key: str = Field(min_length=1, max_length=200)
    type: EventType
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("occurred_at must include an explicit timezone offset")
        return v


class EventBatchIn(BaseModel):
    events: list[EventIn] = Field(min_length=1, max_length=2000)


class BatchResponse(BaseModel):
    received: int
    inserted: int
    duplicates: int
    alerts_created: int
    alert_ids: list[int] = Field(default_factory=list)


class UserOut(BaseModel):
    id: int
    username: str
    full_name: str
    role: UserRole
    org_id: int | None
    manager_id: int | None

    model_config = {"from_attributes": True}


class AlertOut(BaseModel):
    id: int
    rule: AlertRule
    status: AlertStatus
    severity: str
    user_id: int
    device_id: int | None
    window_start: datetime | None
    window_end: datetime | None
    title: str
    evidence: dict[str, Any]
    baseline_id: int | None
    version: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TriageRequest(BaseModel):
    status: AlertStatus
    version: int = Field(ge=1, description="Current alert version the client saw")
    note: str = Field(default="", max_length=4000)


class BaselineOut(BaseModel):
    id: int
    user_id: int
    version: int
    metric: str
    target_date: datetime
    status: BaselineStatus
    days_used: int
    mean: float | None
    stddev: float | None
    daily_counts: dict[str, Any]
    observed_count: int | None
    zscore: float | None
    created_at: datetime

    model_config = {"from_attributes": True}
