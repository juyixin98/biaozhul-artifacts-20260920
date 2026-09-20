from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.addressing import canonical_cidr


# ---------------------------------------------------------------- 通用

class ErrorOut(BaseModel):
    error: str
    message: str


# ---------------------------------------------------------------- 租户

class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class TenantOut(BaseModel):
    id: UUID
    name: str
    created_at: datetime
    # 管理员 API Key 仅在创建时返回一次
    admin_key: str | None = None


# ---------------------------------------------------------------- 接入点

class AccessPointCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    capacity: int = Field(ge=0, le=1_000_000)


class AccessPointOut(BaseModel):
    id: UUID
    tenant_id: UUID
    name: str
    capacity: int
    active_sessions: int = 0
    created_at: datetime


# ---------------------------------------------------------------- 地址池

class AddressPoolCreate(BaseModel):
    cidr: str = Field(examples=["10.10.0.0/24"])
    reserved_first: int = Field(default=0, ge=0, le=1_000_000)
    reserved_last: int = Field(default=0, ge=0, le=1_000_000)

    @field_validator("cidr")
    @classmethod
    def _valid_cidr(cls, v: str) -> str:
        try:
            return canonical_cidr(v.strip())
        except ValueError as exc:
            raise ValueError(f"非法 IPv4 CIDR：{v}（{exc}）") from exc


class AddressPoolOut(BaseModel):
    id: UUID
    tenant_id: UUID
    access_point_id: UUID
    cidr: str
    reserved_first: int
    reserved_last: int
    assignable_count: int = 0
    created_at: datetime


# ---------------------------------------------------------------- 设备

class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class DeviceOut(BaseModel):
    id: UUID
    tenant_id: UUID
    name: str
    status: str
    created_at: datetime
    revoked_at: datetime | None = None
    # 设备令牌仅在创建时返回一次
    token: str | None = None


# ---------------------------------------------------------------- 会话

class ConnectRequest(BaseModel):
    access_point_id: UUID


class LeaseOut(BaseModel):
    lease_id: UUID
    generation: int
    tenant_id: UUID
    access_point_id: UUID
    device_id: UUID
    ip_address: str
    status: str
    connected_at: datetime
    last_seen_at: datetime
    ended_at: datetime | None = None
    termination_reason: str | None = None
    reused: bool = False


class HeartbeatRequest(BaseModel):
    generation: int = Field(ge=1)


class CloseRequest(BaseModel):
    generation: int = Field(ge=1)


# ---------------------------------------------------------------- 管理员查询视图

class LeaseSummary(BaseModel):
    lease_id: UUID
    device_id: UUID
    access_point_id: UUID
    ip_address: str
    generation: int
    status: str
    connected_at: datetime
    last_seen_at: datetime
    ended_at: datetime | None
    termination_reason: str | None
