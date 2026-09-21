from __future__ import annotations

from datetime import datetime, time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models import (
    AssignmentStatus,
    PlanStatus,
    Recurrence,
    TaskStatus,
)


# --- directory --------------------------------------------------------------


class UnitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str


class CoordinatorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    unit_ids: list[int]


class QualificationIn(BaseModel):
    code: str
    valid_from: datetime
    valid_until: datetime


class QualificationOut(QualificationIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


class WorkerIn(BaseModel):
    name: str
    unit_id: int
    timezone: str = "UTC"


class WorkerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    unit_id: int
    timezone: str
    active: bool
    qualifications: list[QualificationOut]


# --- plans ------------------------------------------------------------------


class PlanIn(BaseModel):
    care_recipient_id: str
    unit_id: int
    service_timezone: str = "UTC"
    recurrence: Recurrence
    day_of_week: int | None = Field(default=None, ge=0, le=6)
    window_start: time
    window_end: time
    duration_minutes: int = Field(gt=0)
    required_qualifications: list[str] = Field(default_factory=list)
    # IDs of plans whose same-day tasks must complete before this plan's task.
    prerequisite_plan_ids: list[int] = Field(default_factory=list)


class PlanUpdate(BaseModel):
    """All scheduling fields are optional; any change bumps plan.version.

    Edits only affect not-yet-started tasks (pending / lapsed invitations);
    already accepted assignments are left untouched.
    """

    service_timezone: str | None = None
    recurrence: Recurrence | None = None
    day_of_week: int | None = Field(default=None, ge=0, le=6)
    window_start: time | None = None
    window_end: time | None = None
    duration_minutes: int | None = Field(default=None, gt=0)
    required_qualifications: list[str] | None = None
    prerequisite_plan_ids: list[int] | None = None
    status: PlanStatus | None = None


class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    version: int
    status: PlanStatus
    care_recipient_id: str
    unit_id: int
    service_timezone: str
    recurrence: Recurrence
    day_of_week: int | None
    window_start: time
    window_end: time
    duration_minutes: int
    required_qualifications: list[str]
    prerequisite_plan_ids: list[int]
    created_at: datetime
    updated_at: datetime


class GenerateOut(BaseModel):
    plan_id: int
    horizon_days: int
    created: int
    updated: int
    skipped_locked: int
    cancelled_stale: int
    tasks: list["TaskOut"]


# --- tasks / assignments ----------------------------------------------------


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    plan_id: int
    plan_version: int
    care_recipient_id: str
    unit_id: int
    occurrence_key: str
    starts_at: datetime
    ends_at: datetime
    status: TaskStatus
    prerequisite_task_ids: list[int]


class AssignmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    task_id: int
    worker_id: int
    status: AssignmentStatus
    invited_at: datetime
    expires_at: datetime
    responded_at: datetime | None
    created_via: str
    created_by_coordinator_id: int | None


class ConstraintViolation(BaseModel):
    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class AllocateResult(BaseModel):
    allocated: bool
    task: TaskOut
    assignment: AssignmentOut | None = None
    violations: list[ConstraintViolation] = Field(default_factory=list)


class AcceptIn(BaseModel):
    request_key: str | None = Field(
        default=None,
        description="Idempotency key; repeating the same key never double-books.",
    )


class AcceptOut(BaseModel):
    accepted: bool
    assignment: AssignmentOut | None = None
    violation: ConstraintViolation | None = None
    replayed: bool = False


class ManualAssignIn(BaseModel):
    worker_id: int
    reason: str = Field(min_length=1, description="Required audit reason.")


class RescheduleIn(BaseModel):
    starts_at: datetime
    ends_at: datetime
    reason: str = Field(min_length=1)


class CancelIn(BaseModel):
    reason: str = Field(min_length=1)


class ReaperOut(BaseModel):
    expired: int
    reallocated: int
    results: list[AllocateResult]


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    assignment_id: int | None
    task_id: int
    worker_id: int | None
    action: str
    reason: str | None
    actor: str
    detail: dict[str, Any]
    created_at: datetime


class ErrorOut(BaseModel):
    detail: Any
