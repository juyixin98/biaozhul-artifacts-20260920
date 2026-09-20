from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ConsentEventIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    event_type: Literal["grant", "withdraw"]
    subject_ref: str = Field(min_length=1, max_length=320)
    purpose_code: str = Field(min_length=1, max_length=100)
    policy_version_id: UUID | None = None
    expected_version: int = Field(ge=0)
    expires_at: datetime | None = None


class BatchImportIn(BaseModel):
    items: list[ConsentEventIn] = Field(max_length=500)


class PurposeIn(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)


class PolicyVersionIn(BaseModel):
    version: int = Field(ge=1)
    content: str = Field(min_length=1)
