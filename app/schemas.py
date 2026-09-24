"""Pydantic request/response models."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- auth

class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=256)


class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    operator: str


# ---------------------------------------------------------------- robots

Point2D = tuple[float, float]


class RobotIn(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    name: str
    position: Point2D
    current_soc_kwh: float = Field(ge=0)
    capacity_kwh: float = Field(gt=0)
    payload_capacity_kg: float = Field(default=0, ge=0)


class RobotUpdate(BaseModel):
    name: str | None = None
    position: Point2D | None = None
    current_soc_kwh: float | None = Field(default=None, ge=0)


# ---------------------------------------------------------------- chargers

class ChargerIn(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    name: str
    position: Point2D
    capacity: int = Field(default=1, ge=1)


# ---------------------------------------------------------------- tasks

class TaskIn(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    title: str
    pickup: Point2D
    delivery: Point2D
    payload_kg: float = Field(ge=0)
    wait_seconds: float = Field(default=0, ge=0)
    priority: int = Field(default=0, ge=0, le=10)


class CostPreviewIn(BaseModel):
    robot_pos: Point2D
    current_soc_kwh: float = Field(ge=0)
    capacity_kwh: float = Field(gt=0)
    pickup: Point2D
    delivery: Point2D
    payload_kg: float = Field(ge=0)
    wait_seconds: float = Field(default=0, ge=0)
    priority: int = Field(default=0, ge=0, le=10)


# ---------------------------------------------------------------- runtime

class TelemetryIn(BaseModel):
    measured_soc_kwh: float = Field(ge=0)
    note: str | None = None


class DispatchResult(BaseModel):
    assignments: list[dict[str, Any]]
    unassigned: list[dict[str, Any]]
    blocked: list[dict[str, Any]]
    simulated: bool = True
