from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(pattern="^(supervisor|learner)$")


class UserOut(BaseModel):
    id: int
    name: str
    role: str


# ---------------------------------------------------------------------------
# Programs / versions
# ---------------------------------------------------------------------------

class StepIn(BaseModel):
    key: str = Field(min_length=1, max_length=60)
    position: int = Field(ge=1, le=50)
    instruction: str = Field(min_length=1)
    pass_condition: str = Field(min_length=1)
    prerequisite_keys: list[str] = Field(default_factory=list)


class ProgramCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = ""


class VersionCreate(BaseModel):
    steps: list[StepIn] = Field(min_length=1, max_length=50)
    publish: bool = True


class RollbackIn(BaseModel):
    version_id: int


# ---------------------------------------------------------------------------
# Courses / enrolment
# ---------------------------------------------------------------------------

class CourseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    version_id: int
    capacity: int = Field(ge=0)
    enroll_deadline: datetime


class EnrollmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    course_id: int
    learner_id: int
    version_id: int
    status: str
    waitlist_position: int | None
    offered_at: datetime | None
    seat_expires_at: datetime | None
    confirmed_at: datetime | None
    cancelled_at: datetime | None
    completed_at: datetime | None
    created_at: datetime | None


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------

class SubmissionIn(BaseModel):
    content: str = Field(min_length=1)


class ReviewIn(BaseModel):
    passed: bool


class CorrectionIn(BaseModel):
    new_status: str = Field(pattern="^(passed|failed)$")
    reason: str = Field(min_length=1)


class StepResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    enrollment_id: int
    step_id: int
    status: str
    submission_content: str
    attempts: int
    last_submitted_at: datetime | None
    reviewed_by: int | None
    reviewed_at: datetime | None
    corrected: bool


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------

class FreezeIn(BaseModel):
    at: datetime


class AdvanceIn(BaseModel):
    seconds: float = Field(ge=0)
