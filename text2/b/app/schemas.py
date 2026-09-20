"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ---- organisation -----------------------------------------------------------

class UnitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    timezone: str


class WorkerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    external_id: str
    timezone: str
    active: bool


class CoordinatorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    external_id: str
    active: bool
    unit_ids: list[int]


# ---- plans ------------------------------------------------------------------

class SlotIn(BaseModel):
    weekday: int = Field(ge=0, le=6, description="0=Monday .. 6=Sunday")
    start_at: str = Field(description="Local wall-clock time HH:MM")
    latest_start_at: str = Field(description="Window end HH:MM (may be earlier if crossing midnight)")
    duration_minutes: int = Field(gt=0, le=1440)


class SlotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    weekday: int
    start_at: datetime
    latest_start_at: datetime
    duration_minutes: int


class PlanCreate(BaseModel):
    unit_id: int
    title: str
    timezone: str = "UTC"
    period_days: int = Field(7, ge=1)
    anchor_date: date
    slots: list[SlotIn] = Field(min_length=1)
    qualification_ids: list[int] = Field(default_factory=list)
    prerequisite_plan_ids: list[int] = Field(default_factory=list)


class PlanRevise(BaseModel):
    timezone: str | None = None
    period_days: int | None = Field(default=None, ge=1)
    anchor_date: date | None = None
    slots: list[SlotIn] | None = None
    qualification_ids: list[int] | None = None
    prerequisite_plan_ids: list[int] | None = None
    change_note: str = ""


class PlanVersionOut(BaseModel):
    id: int
    version_number: int
    status: str
    timezone: str
    period_days: int
    anchor_date: datetime
    change_note: str | None = None
    created_at: datetime
    qualification_ids: list[int]
    prerequisite_plan_ids: list[int]
    slots: list[SlotOut]


class PlanOut(BaseModel):
    id: int
    unit_id: int
    title: str
    active: bool
    created_at: datetime
    active_version_number: int | None
    versions: list[PlanVersionOut]


# ---- tasks ------------------------------------------------------------------

class TaskOut(BaseModel):
    id: int
    plan_id: int
    plan_version_id: int
    occurrence_date: date
    starts_at: datetime
    ends_at: datetime
    latest_start_at: datetime
    duration_minutes: int
    status: str
    manually_adjusted: bool
    required_qualification_ids: list[int]
    prerequisite_task_ids: list[int]


class GenerateResponse(BaseModel):
    plan_id: int
    horizon_days: int
    tasks: list[TaskOut]


class ViolationOut(BaseModel):
    code: str
    message: str
    context: dict[str, Any] = Field(default_factory=dict)


class CandidateOut(BaseModel):
    worker_id: int
    worker_name: str
    feasible: bool
    violations: list[ViolationOut]
    min_remaining_minutes: int
    total_load_minutes: int


class ScheduleResponse(BaseModel):
    task_id: int
    status: str
    assignment_id: int | None
    worker_id: int | None
    violations: list[dict[str, Any]] = Field(default_factory=list)


# ---- assignments ------------------------------------------------------------

class AssignmentOut(BaseModel):
    id: int
    task_id: int
    worker_id: int
    status: str
    invited_at: datetime
    expires_at: datetime
    responded_at: datetime | None
    invited_by: str
    task_starts_at: datetime
    task_ends_at: datetime


class AcceptResponse(BaseModel):
    assignment: AssignmentOut
    task_status: str


class ManualAssignIn(BaseModel):
    worker_id: int
    reason: str = Field(min_length=1, max=500)


class ManualUnassignIn(BaseModel):
    reason: str = Field(min_length=1, max=500)


class ManualRescheduleIn(BaseModel):
    starts_at: datetime = Field(description="Timezone-aware ISO datetime")
    duration_minutes: int = Field(gt=0, le=1440)
    reason: str = Field(min_length=1, max=500)


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    task_id: int | None
    assignment_id: int | None
    worker_id: int | None
    coordinator_id: int | None
    event_type: str
    detail: str | None
    created_at: datetime


# ---- admin/seed -------------------------------------------------------------

class CompleteTaskIn(BaseModel):
    pass
