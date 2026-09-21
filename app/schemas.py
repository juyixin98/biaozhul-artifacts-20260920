"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


# ---------- directory / org ----------

class UnitIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class UnitOut(BaseModel):
    id: int
    name: str


class CoordinatorIn(BaseModel):
    name: str
    is_admin: bool = False


class CoordinatorOut(BaseModel):
    id: int
    name: str
    is_admin: bool
    granted_unit_ids: list[int] = []


class GrantIn(BaseModel):
    unit_id: int


class WorkerIn(BaseModel):
    name: str
    unit_id: int
    active: bool = True


class WorkerOut(BaseModel):
    id: int
    name: str
    unit_id: int
    active: bool


class QualificationIn(BaseModel):
    code: str
    name: str | None = None


class WorkerQualificationIn(BaseModel):
    code: str
    valid_from: datetime
    valid_until: datetime | None = None

    @field_validator("valid_from", "valid_until")
    @classmethod
    def _tzaware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC3339)")
        return v


# ---------- plans ----------

class TemplateIn(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    window_start_minute: int = Field(ge=0, le=1439)
    window_end_minute: int = Field(ge=1, le=1440)
    duration_minutes: int = Field(ge=1)
    weekday_mask: list[int] = Field(default_factory=list)
    qualification_codes: list[str] = Field(default_factory=list)
    prerequisite_codes: list[str] = Field(default_factory=list)

    @field_validator("weekday_mask")
    @classmethod
    def _weekdays(cls, v: list[int]) -> list[int]:
        if any(d not in range(1, 8) for d in v):
            raise ValueError("weekday_mask only allows 1..7 (Mon..Sun)")
        return v


class PlanCreateIn(BaseModel):
    external_id: str = Field(min_length=1, max_length=100)
    client_name: str
    unit_id: int
    timezone: str = "Asia/Shanghai"
    templates: list[TemplateIn] = Field(min_length=1)


class PlanReviseIn(BaseModel):
    client_name: str
    timezone: str = "Asia/Shanghai"
    templates: list[TemplateIn] = Field(min_length=1)


class PlanOut(BaseModel):
    id: int
    external_id: str
    revision: int
    active: bool
    client_name: str
    unit_id: int
    timezone: str
    created_at: datetime
    generated_task_count: int | None = None


# ---------- tasks ----------

class TaskOut(BaseModel):
    id: int
    plan_id: int
    unit_id: int
    scheduled_date: str
    scheduled_start: datetime
    scheduled_end: datetime
    status: str
    qualification_codes: list[str]
    prerequisites: list[dict]
    assigned_worker_id: int | None = None
    active_invitation_id: int | None = None


class ViolationOut(BaseModel):
    worker_id: int | None = None
    constraint: str
    message: str
    detail: dict | None = None


class AllocateOut(BaseModel):
    task_id: int
    status: Literal["invited", "assigned", "unassigned", "unchanged"]
    worker_id: int | None = None
    invitation_id: int | None = None
    round: int | None = None
    violations: list[dict] = []


class BulkAllocateOut(BaseModel):
    results: list[AllocateOut]


class ManualAssignIn(BaseModel):
    worker_id: int
    reason: str = Field(min_length=1, max=1000)


class ManualRescheduleIn(BaseModel):
    scheduled_start: datetime
    scheduled_end: datetime
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("scheduled_start", "scheduled_end")
    @classmethod
    def _tzaware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC3339)")
        return v


class ReasonIn(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class InvitationOut(BaseModel):
    id: int
    task_id: int
    worker_id: int
    round: int
    status: str
    created_at: datetime
    expires_at: datetime
    responded_at: datetime | None = None


class AuditOut(BaseModel):
    id: int
    task_id: int | None
    actor_type: str
    actor_id: str
    action: str
    reason: str | None
    detail: dict
    created_at: datetime


class WeeklyHoursOut(BaseModel):
    worker_id: int
    weeks: dict[str, float]  # ISO week start (UTC) -> hours in that week


class ClockIn(BaseModel):
    # freeze at a tz-aware instant, or advance by a number of seconds from now.
    freeze_at: datetime | None = None
    advance_seconds: float | None = None
    reset: bool = False

    @field_validator("freeze_at")
    @classmethod
    def _tzaware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("freeze_at must be timezone-aware (RFC3339)")
        return v


class ClockOut(BaseModel):
    now: datetime
