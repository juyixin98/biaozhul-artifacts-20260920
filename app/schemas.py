"""API 请求/响应模型。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Action = Literal["OPEN", "ORACLE", "RATE_SCHEDULE", "REPAY", "LIQUIDATE"]


class EventIn(BaseModel):
    event_id: str = Field(min_length=1)
    ts: int = Field(ge=0)
    seq: int = Field(ge=0)
    action: Action
    payload: dict[str, Any]
    signer: str = Field(min_length=32, max_length=64, description="签名者公钥(hex)")
    signature: str = Field(min_length=1, description="对其余字段规范化JSON的Ed25519签名(hex)")


class IngestResult(BaseModel):
    accepted: int
    duplicates: list[str]
    rejected: list[dict[str, Any]]
    latest_version: int | None
    previous_version: int | None
    report_changed: bool
    hash: str | None
    changed_reasons: list[str]


class ReportSummary(BaseModel):
    version: int
    created_at: str
    hash: str
    event_count: int
    as_of: int
    change_reason: str
    signature: str


class VerifyResult(BaseModel):
    report_signature_valid: bool
    signer_hex: str
    hash_matches_body: bool


class Health(BaseModel):
    status: str
    db: str
    latest_version: int | None
