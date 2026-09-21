import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------- requests ----------

class StepIn(BaseModel):
    step_key: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str = Field(min_length=1, max_length=200)
    instructions: str = ""
    pass_condition: str = ""
    prerequisites: list[str] = []


class ProgramCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    steps: list[StepIn]
    change_note: str = "initial draft"


class VersionCreate(BaseModel):
    steps: list[StepIn]
    change_note: str = ""


class StepsReplace(BaseModel):
    steps: list[StepIn]


class RollbackRequest(BaseModel):
    version_number: int


class CourseCreate(BaseModel):
    program_id: uuid.UUID
    title: str = Field(min_length=1, max_length=200)
    capacity: int = Field(ge=1)
    enrollment_deadline: datetime


class EnrollRequest(BaseModel):
    learner_id: str = Field(min_length=1, max_length=100)
    version_id: Optional[uuid.UUID] = None  # defaults to the program's current version


class ResultSubmit(BaseModel):
    step_key: str
    passed: bool


class CorrectionCreate(BaseModel):
    passed: bool
    reason: str

    @field_validator("reason")
    @classmethod
    def reason_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("correction reason must not be empty")
        return v


# ---------- responses ----------

class StepOut(BaseModel):
    step_key: str
    order_index: int
    title: str
    instructions: str
    pass_condition: str
    prerequisites: list[str]

    model_config = ConfigDict(from_attributes=True)


class VersionOut(BaseModel):
    id: uuid.UUID
    program_id: uuid.UUID
    version_number: int
    status: str
    change_note: str
    published_at: Optional[datetime]
    steps: list[StepOut]

    model_config = ConfigDict(from_attributes=True)


class ProgramOut(BaseModel):
    id: uuid.UUID
    title: str
    description: str
    created_by: str
    current_version_id: Optional[uuid.UUID]

    model_config = ConfigDict(from_attributes=True)


class CourseOut(BaseModel):
    id: uuid.UUID
    program_id: uuid.UUID
    title: str
    capacity: int
    seats_taken: int
    enrollment_deadline: datetime

    model_config = ConfigDict(from_attributes=True)


class EnrollmentOut(BaseModel):
    id: uuid.UUID
    course_id: uuid.UUID
    learner_id: str
    version_id: uuid.UUID
    status: str
    confirm_deadline: Optional[datetime]
    confirmed_at: Optional[datetime]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ResultOut(BaseModel):
    id: uuid.UUID
    enrollment_id: uuid.UUID
    step_key: str
    passed: bool
    source: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CertificateOut(BaseModel):
    id: uuid.UUID
    enrollment_id: uuid.UUID
    version_id: uuid.UUID
    serial_number: str
    content_digest: str
    status: str
    issued_at: datetime
    revoked_at: Optional[datetime]

    model_config = ConfigDict(from_attributes=True)
