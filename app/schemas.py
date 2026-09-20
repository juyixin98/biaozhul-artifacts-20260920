from __future__ import annotations

import ipaddress
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


# --- auth ---


class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AdminCreateIn(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=6, max_length=256)


class AdminOut(BaseModel):
    id: str
    username: str
    tenant_id: str | None


# --- tenants ---


class TenantIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class TenantOut(BaseModel):
    id: str
    name: str
    created_at: datetime


# --- pools ---


class PoolIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    cidr: str
    reserved: list[str] = Field(default_factory=list)

    @field_validator("cidr")
    @classmethod
    def _cidr_ok(cls, v: str) -> str:
        try:
            net = ipaddress.ip_network(v, strict=False)
        except ValueError as exc:
            raise ValueError(f"invalid CIDR: {v}") from exc
        if not isinstance(net, ipaddress.IPv4Network):
            raise ValueError("only IPv4 pools are supported")
        return str(net)

    @field_validator("reserved")
    @classmethod
    def _reserved_ok(cls, v: list[str]) -> list[str]:
        out = []
        for item in v:
            try:
                addr = ipaddress.ip_address(item)
            except ValueError as exc:
                raise ValueError(f"invalid reserved address: {item}") from exc
            if not isinstance(addr, ipaddress.IPv4Address):
                raise ValueError(f"reserved address must be IPv4: {item}")
            out.append(str(addr))
        return out


class PoolOut(BaseModel):
    id: str
    tenant_id: str
    name: str
    cidr: str
    reserved: list[str]
    usable_addresses: int
    allocated_addresses: int
    created_at: datetime


# --- access points ---


class AccessPointIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    pool_id: str
    capacity: int = Field(ge=1, le=1_000_000)


class AccessPointOut(BaseModel):
    id: str
    tenant_id: str
    name: str
    pool_id: str
    capacity: int
    active_sessions: int
    created_at: datetime


# --- devices ---


class DeviceIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class DeviceOut(BaseModel):
    id: str
    tenant_id: str
    name: str
    token_prefix: str
    revoked: bool
    generation: int
    created_at: datetime


class DeviceCreatedOut(DeviceOut):
    token: str  # returned exactly once


# --- leases / sessions ---


class ConnectIn(BaseModel):
    access_point_id: str
    idempotency_key: str = Field(min_length=1, max_length=128)


class ConnectOut(BaseModel):
    lease_id: str
    device_id: str
    access_point_id: str
    ip: str
    generation: int
    state: str
    reused: bool  # True when an idempotent replay returned the existing lease
    heartbeat_interval_seconds: int
    expires_at: datetime


class HeartbeatIn(BaseModel):
    lease_id: str
    generation: int


class HeartbeatOut(BaseModel):
    lease_id: str
    state: str
    expires_at: datetime


class DisconnectIn(BaseModel):
    lease_id: str
    generation: int


class LeaseOut(BaseModel):
    id: str
    tenant_id: str
    access_point_id: str
    pool_id: str
    device_id: str
    ip: str
    generation: int
    state: str
    created_at: datetime
    last_heartbeat_at: datetime
    expires_at: datetime
    released_at: datetime | None
    release_reason: str | None


class TerminationOut(BaseModel):
    lease_id: str
    device_id: str
    access_point_id: str
    ip: str
    released_at: datetime
    release_reason: str
