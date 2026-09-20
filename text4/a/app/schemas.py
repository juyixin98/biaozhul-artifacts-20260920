from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, Field, field_validator

from .models import EnrollmentStatus, ResultStatus, UserRole, VersionStatus

# EmailStr needs an email validator; avoid extra dependency by using str with
# a light check at the boundary instead.


class ORMModel(BaseModel):
    model_config = {"from_attributes": True}


# ---------- users ----------


class UserCreate(BaseModel):
    email: str
    name: str
    role: UserRole

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        local, _, domain = v.partition("@")
        if not local or not domain or "." not in domain:
            raise ValueError("invalid email")
        return v


class UserOut(ORMModel):
    id: int
    email: str
    name: str
    role: UserRole


# ---------- programs / versions / steps ----------


class StepIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = ""
    pass_criteria: str = Field(min_length=1)
    # Orders of other steps that must pass first; empty means no prerequisite.
    prerequisite_orders: list[int] = Field(default_factory=list)


class VersionCreate(BaseModel):
    steps: list[StepIn] = Field(min_length=1, max_length=50)

    @field_validator("steps")
    @classmethod
    def _orders_cover_all(cls, steps: list[StepIn]) -> list[StepIn]:
        return steps


class StepOut(ORMModel):
    order_index: int
    title: str
    description: str
    pass_criteria: str
    prerequisite_orders: list[int]


class ProgramCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = ""


class ProgramOut(ORMModel):
    id: int
    title: str
    description: str
    created_by: int
    current_version_id: int | None = None


class VersionOut(ORMModel):
    id: int
    program_id: int
    version: int
    status: VersionStatus
    content_hash: str | None
    created_at: datetime
    published_at: datetime | None
    steps: list[StepOut]


# ---------- courses / enrollments ----------


class CourseCreate(BaseModel):
    version_id: int
    capacity: int = Field(gt=0, le=100_000)
    enrollment_deadline: datetime


class CourseOut(ORMModel):
    id: int
    version_id: int
    capacity: int
    enrollment_deadline: datetime
    seats_taken: int
    waiting_count: int


class EnrollmentOut(ORMModel):
    id: int
    learner_id: int
    version_id: int
    course_id: int
    status: EnrollmentStatus
    seat_number: int | None
    waitlist_position: int | None
    seat_expires_at: datetime | None
    confirmed_at: datetime | None
    created_at: datetime
    updated_at: datetime


# ---------- step results / certificates ----------


class SubmissionCreate(BaseModel):
    content: str = Field(default="", max_length=10_000)
    claimed_passed: bool = False


class CorrectionCreate(BaseModel):
    status: ResultStatus
    reason: str = Field(min_length=1, max_length=2000)


class StepResultOut(ORMModel):
    step_id: int
    step_order: int
    status: ResultStatus
    attempt_count: int
    passed_at: datetime | None
    correction_reason: str | None
    corrected_at: datetime | None


class ProgressOut(BaseModel):
    enrollment_id: int
    version_id: int
    status: EnrollmentStatus
    results: list[StepResultOut]
    total_steps: int
    passed_steps: int
    certificate_id: int | None
    certificate_status: str | None
    certificate_serial: str | None


class CertificateOut(ORMModel):
    id: int
    enrollment_id: int
    version_id: int
    serial: str
    learner_name: str
    program_title: str
    version_number: int
    content_hash: str
    status: str
    issued_at: datetime
    revoked_at: datetime | None
    revoke_reason: str | None


# ---------- clock / admin ----------


class ClockSet(BaseModel):
    now: datetime


class ClockAdvance(BaseModel):
    minutes: int = 0
    hours: int = 0
    days: int = 0

    def delta(self) -> timedelta:
        return timedelta(minutes=self.minutes, hours=self.hours, days=self.days)
