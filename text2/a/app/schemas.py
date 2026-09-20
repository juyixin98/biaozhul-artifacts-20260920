from datetime import date, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, field_validator


class PlanCreate(BaseModel):
    name: str
    unit_id: str
    patient_name: str
    timezone: str = "UTC"
    frequency: Literal["daily", "weekly"]
    interval: int = 1
    byweekday: list[str] | None = None
    window_start: time
    window_end: time
    duration_minutes: int
    required_qualification: str
    prerequisite_plan_id: int | None = None
    start_date: date

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception:
            raise ValueError(f"unknown IANA timezone: {v}")
        return v


class PlanRevise(BaseModel):
    """计划改版：只影响尚未开始的任务。"""

    name: str | None = None
    timezone: str | None = None
    frequency: Literal["daily", "weekly"] | None = None
    interval: int | None = None
    byweekday: list[str] | None = None
    window_start: time | None = None
    window_end: time | None = None
    duration_minutes: int | None = None
    required_qualification: str | None = None
    prerequisite_plan_id: int | None = None
    start_date: date | None = None
    active: bool | None = None

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                ZoneInfo(v)
            except Exception:
                raise ValueError(f"unknown IANA timezone: {v}")
        return v


class PlanOut(PlanCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    version: int
    active: bool


class CaregiverCreate(BaseModel):
    name: str
    unit_id: str


class CaregiverOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    unit_id: str


class QualificationCreate(BaseModel):
    code: str
    valid_from: datetime
    valid_until: datetime


class QualificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    caregiver_id: int
    code: str
    valid_from: datetime
    valid_until: datetime


class CoordinatorCreate(BaseModel):
    name: str
    unit_ids: list[str]


class CoordinatorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    plan_id: int
    plan_version: int
    scheduled_start: datetime
    scheduled_end: datetime
    status: str
    required_qualification: str
    unit_id: str
    depends_on_task_id: int | None


class OfferCreate(BaseModel):
    caregiver_id: int | None = None  # 为空则自动挑选最优候选人


class OfferOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    caregiver_id: int
    status: str
    offered_at: datetime
    expires_at: datetime


class OfferRespond(BaseModel):
    caregiver_id: int


class AssignmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    caregiver_id: int
    source: str
    created_by: str
    created_at: datetime


class AdjustBody(BaseModel):
    coordinator_id: int
    caregiver_id: int
    reason: str


class AdjustmentLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    actor: str
    action: str
    reason: str | None
    details: dict | None
    created_at: datetime
