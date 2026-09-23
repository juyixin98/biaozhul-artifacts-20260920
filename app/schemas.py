"""Pydantic API 模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ChainIn(BaseModel):
    chain_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")
    name: str = ""
    confirmation_depth: int = Field(default=0, ge=0)
    message_timeout_seconds: int = Field(default=86400, ge=1)


class FeederIn(BaseModel):
    public_key_hex: str = Field(min_length=64, max_length=64)
    label: str = ""


class BlockIn(BaseModel):
    block_height: int = Field(ge=0)
    block_hash: str = Field(min_length=1, max_length=66)
    parent_hash: str = Field(min_length=1, max_length=66)
    block_time: datetime


class BlocksIn(BaseModel):
    tip_block_hash: str | None = None
    blocks: list[BlockIn]


class EventEnvelopeIn(BaseModel):
    """feeder 投递的单条事件信封：事件载荷 + 对规范载荷的 Ed25519 签名。"""
    payload: dict[str, Any]
    signature_hex: str
    feeder_public_key_hex: str


class EventsBatchIn(BaseModel):
    events: list[EventEnvelopeIn]


class EventResult(BaseModel):
    index: int
    status: str
    event_key: str | None = None
    delivery_count: int | None = None
    payload_conflict: bool | None = None
    error: str | None = None


class ReconcileIn(BaseModel):
    as_of: datetime | None = None


class FindingOut(BaseModel):
    code: str
    severity: str
    message_id: str | None
    asset_uid: str | None
    detail: str
    evidence_path: str


class ReconciliationOut(BaseModel):
    run_id: str
    snapshot_hash: str
    prev_snapshot_hash: str
    snapshot_path: str
    as_of: str
    finding_count: int
    findings: list[FindingOut] = []


class HealthOut(BaseModel):
    status: str
    database: str
