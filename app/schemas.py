from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

EVENT_TYPES = ("access", "file_access", "file_download", "usb_connect")
ALERT_STATUSES = ("open", "confirmed", "false_positive", "investigated")


class EventIn(BaseModel):
    device_id: int
    event_id: str = Field(min_length=1, max_length=128)
    event_type: Literal["access", "file_access", "file_download", "usb_connect"]
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def must_be_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
            raise ValueError("occurred_at must include a timezone offset")
        return v


class BatchIn(BaseModel):
    events: list[EventIn] = Field(min_length=1, max_length=2000)


class BatchResult(BaseModel):
    inserted: int
    duplicates: int
    alerts_created: int


class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AlertPatch(BaseModel):
    status: Literal["open", "confirmed", "false_positive", "investigated"]
    version: int = Field(ge=1)


class AlertOut(BaseModel):
    id: int
    employee_id: int
    employee_name: str
    department_id: int
    rule: str
    window_start: datetime
    window_end: datetime
    status: str
    version: int
    evidence: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class BaselineOut(BaseModel):
    id: int
    employee_id: int
    baseline_date: Any
    version: int
    day_count: int
    mean: float | None
    std: float | None
    status: str
    observed_count: int
    z_score: float | None
    created_at: datetime

    model_config = {"from_attributes": True}
