from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    tenant_id: uuid.UUID | None = None
    is_platform_admin: bool


# ---------------------------------------------------------------------------
# Platform admin
# ---------------------------------------------------------------------------

class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class TenantOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: dt.datetime


class TenantAdminCreate(BaseModel):
    username: str = Field(min_length=3, max_length=128)
    password: str = Field(min_length=8, max_length=128)


class AdminOut(BaseModel):
    id: uuid.UUID
    username: str
    tenant_id: uuid.UUID | None
    is_platform_admin: bool


# ---------------------------------------------------------------------------
# Pools / access points / devices
# ---------------------------------------------------------------------------

class PoolCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    cidr: str
    reserved_addresses: list[str] = Field(default_factory=list)


class PoolOut(BaseModel):
    id: uuid.UUID
    name: str
    cidr: str
    total_addresses: int
    usable_addresses: int
    reserved_addresses: int
    allocated_addresses: int
    created_at: dt.datetime


class AccessPointCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    pool_id: uuid.UUID
    capacity: int = Field(gt=0)


class AccessPointOut(BaseModel):
    id: uuid.UUID
    name: str
    pool_id: uuid.UUID
    capacity: int
    active_sessions: int
    created_at: dt.datetime


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class DeviceOut(BaseModel):
    id: uuid.UUID
    name: str
    revoked: bool
    created_at: dt.datetime


class DeviceEnrolledOut(DeviceOut):
    """Returned at creation / token rotation. The token is shown exactly once."""

    device_token: str


# ---------------------------------------------------------------------------
# Device-facing lease API
# ---------------------------------------------------------------------------

class ConnectRequest(BaseModel):
    access_point_id: uuid.UUID
    idempotency_key: str | None = Field(default=None, max_length=128)

    @field_validator("idempotency_key")
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            return None
        return v


class SessionOut(BaseModel):
    id: uuid.UUID
    generation: int
    status: str
    ip: str
    access_point_id: uuid.UUID
    device_id: uuid.UUID
    connected_at: dt.datetime
    last_heartbeat_at: dt.datetime
    closed_at: dt.datetime | None = None


class ConnectResponse(BaseModel):
    session: SessionOut
    lease_token: str
    reconnected: bool
    idempotent_reused: bool


class HeartbeatRequest(BaseModel):
    session_id: uuid.UUID
    generation: int = Field(ge=1)


class CloseRequest(BaseModel):
    session_id: uuid.UUID
    generation: int = Field(ge=1)


# ---------------------------------------------------------------------------
# Termination audit
# ---------------------------------------------------------------------------

class TerminationOut(BaseModel):
    session_id: uuid.UUID
    reason: str
    detail: str | None
    terminated_at: dt.datetime
