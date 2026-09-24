"""Pydantic request/response models for the HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class RegisterRequest(BaseModel):
    namespace: str = Field(
        ..., description="Relative ROS 2 namespace body, e.g. 'team/alpha'."
    )
    prewarm_topics: list[str] = Field(
        default_factory=list,
        description="Optional relative topics whose latched publishers are "
                    "created immediately so DDS endpoint matching settles "
                    "before the first command (e.g. ['cmd/move']).",
    )


class IssueTokenRequest(BaseModel):
    tester_id: str = Field(
        ..., min_length=1, max_length=64,
        description="Test identity bound to the token and to every command.",
    )
    ttl_seconds: int | None = Field(
        default=None, ge=1, le=24 * 3600,
        description="Optional token lifetime; defaults to server configuration.",
    )

    @field_validator("tester_id")
    @classmethod
    def _tester_charset(cls, v: str) -> str:
        allowed = set(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.@")
        if any(c not in allowed for c in v):
            raise ValueError(
                "tester_id may contain letters, digits and _-.@ only")
        return v


class CommandRequest(BaseModel):
    target: str = Field(
        ...,
        description="Relative target topic inside the robot namespace, "
                    "e.g. 'cmd/move'. Absolute paths are rejected.",
    )
    sequence: int = Field(..., ge=0, description="Monotonic per-target number.")
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary application command body (really sent over ROS).",
    )
    # Either ttl_seconds or expires_at may describe validity. When both are
    # given they must agree; expires_at is an absolute unix timestamp.
    ttl_seconds: float | None = Field(default=None, gt=0, le=24 * 3600)
    expires_at: float | None = Field(default=None, gt=0)
    tester_id: str | None = Field(
        default=None,
        description="Must equal the test identity the token was issued for.",
    )


class CommandResponse(BaseModel):
    status: Literal["published"]
    robot_id: str
    namespace: str
    topic: str
    sequence: int
    epoch: int
    tester_id: str
    expires_at: float
    subscriber_count: int
    published_at: float


class TokenResponse(BaseModel):
    token: str
    robot_id: str
    tester_id: str
    epoch: int
    expires_at: int


class RobotView(BaseModel):
    robot_id: str
    namespace: str
    epoch: int
    watermarks: dict[str, int]
    created_at: float
    updated_at: float


class ErrorResponse(BaseModel):
    error: str
    reason: str
    detail: dict[str, Any] = Field(default_factory=dict)
