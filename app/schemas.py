from __future__ import annotations

import ipaddress
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------- super admin ----------

class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    # If omitted the server generates an admin key and returns it once.
    admin_key: str | None = Field(default=None, min_length=8, max_length=128)


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    created_at: datetime
    admin_key: str | None = None  # populated only on create


# ---------- tenant admin ----------

class PoolCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    cidr: str
    reserved_ips: list[str] = Field(default_factory=list)

    @field_validator("cidr")
    @classmethod
    def _valid_cidr(cls, v: str) -> str:
        try:
            net = ipaddress.ip_network(v, strict=True)
        except ValueError as exc:
            raise ValueError("cidr must be a valid network in address/prefix form") from exc
        if net.version != 4:
            raise ValueError("only IPv4 pools are supported")
        return v

    @field_validator("reserved_ips")
    @classmethod
    def _valid_reserved(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        for ip in v:
            value = ipaddress.ip_address(ip)
            if value.version != 4:
                raise ValueError(f"reserved address {ip} is not IPv4")
            if ip in seen:
                raise ValueError(f"reserved address {ip} listed more than once")
            seen.add(ip)
        return v

    @model_validator(mode="after")
    def _reserved_in_network(self):
        net = ipaddress.ip_network(self.cidr)
        for ip in self.reserved_ips:
            if ipaddress.ip_address(ip) not in net:
                raise ValueError(f"reserved address {ip} is outside {self.cidr}")
        return self


class PoolOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    cidr: str
    reserved_ips: list[str] = []
    total_hosts: int = 0
    reserved_count: int = 0
    usable: int = 0
    created_at: datetime


class AccessPointCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    pool_id: int
    capacity: int = Field(gt=0, le=100_000)


class AccessPointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    pool_id: int
    capacity: int
    active_leases: int = 0
    created_at: datetime


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    revoked: bool
    created_at: datetime
    device_token: str | None = None  # populated only on create


class LeaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    device_id: int
    access_point_id: int
    ip_address: str
    generation: int
    status: str
    last_heartbeat_at: datetime
    expires_at: datetime
    closed_at: datetime | None
    close_reason: str | None
    created_at: datetime


class SessionOut(BaseModel):
    """Returned by device connect: lease plus the session token for heartbeats."""

    lease: LeaseOut
    session_token: str
    heartbeat_interval_seconds: int


# ---------- device ----------

class ConnectRequest(BaseModel):
    access_point_id: int
    idempotency_key: str | None = Field(default=None, max_length=128)


class HeartbeatResponse(BaseModel):
    lease_id: int
    generation: int
    last_heartbeat_at: datetime
    expires_at: datetime
