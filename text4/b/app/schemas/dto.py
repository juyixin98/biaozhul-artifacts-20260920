"""Pydantic request/response models."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["supervisor", "learner"]


# ---------- auth ----------

class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    login: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=6, max_length=200)
    role: Role


class LoginRequest(BaseModel):
    login: str
    password: str


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    login: str
    role: str


class TokenResponse(BaseModel):
    token: str
    user: UserResponse


# ---------- programs / versions / steps ----------

class ProgramCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    capacity: int = Field(ge=0)
    enrollment_deadline: datetime


class ProgramResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    description: str
    supervisor_id: int
    capacity: int
    enrollment_deadline: datetime
    current_version_id: Optional[int] = None
    created_at: datetime


class StepIn(BaseModel):
    # Steps are supplied in list order; ``position`` is implicit (1-based).
    title: str = Field(min_length=1, max_length=200)
    instruction: str = Field(min_length=1)
    pass_condition: str = Field(min_length=1)
    # 1-based positions that must be passed before this step.
    prerequisite_positions: list[int] = Field(default_factory=list)


class DraftCreate(BaseModel):
    # When provided the draft is seeded with an existing version's steps;
    # otherwise it starts empty.
    base_version_id: Optional[int] = None


class ReplaceStepsRequest(BaseModel):
    steps: list[StepIn] = Field(min_length=1, max_length=50)


class StepResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    position: int
    title: str
    instruction: str
    pass_condition: str
    prerequisite_positions: list[int]


class VersionResponse(BaseModel):
    id: int
    program_id: int
    version_number: int
    status: str
    created_at: datetime
    published_at: Optional[datetime]
    steps: list[StepResponse]


# ---------- enrollments ----------

class EnrollmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    program_id: int
    version_id: int
    learner_id: int
    status: str
    waitlist_position: Optional[int]
    created_at: datetime
    seat_granted_at: Optional[datetime]
    confirmed_at: Optional[datetime]


# ---------- step results ----------

class SubmissionCreate(BaseModel):
    content: str = ""


class StepResultResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    enrollment_id: int
    step_id: int
    status: str
    content: str
    submitted_at: datetime
    evaluated_at: Optional[datetime]
    last_correction_reason: Optional[str]


class CorrectionRequest(BaseModel):
    status: Literal["passed", "failed"]
    reason: str = Field(min_length=1)


# ---------- certificates ----------

class CertificateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    enrollment_id: int
    version_id: int
    serial_number: str
    content_digest: str
    revoked: bool
    issued_at: datetime
    revoked_at: Optional[datetime]
