"""API 层 Pydantic 模型：事件信封、批量提交、报告视图。

金额字段在提交时允许十进制字符串/整数，服务层统一解析为 WAD 整数。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

EventType = Literal[
    "rate_schedule",
    "open_position",
    "deposit",
    "withdraw",
    "borrow",
    "repay",
    "price_update",
    "liquidate",
]


class EventIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    ts: int = Field(ge=0, description="事件发生的协议时间（整数秒）")
    type: EventType
    payload: dict[str, Any]
    sig: Optional[str] = Field(None, description="canonical 信封的 Ed25519 签名（hex）")


class EventBatchIn(BaseModel):
    events: list[EventIn] = Field(min_length=1)


class EventView(BaseModel):
    seq: int
    event_id: str
    ts: int
    type: str
    payload: dict[str, Any]
    sig: Optional[str]
    late: bool
    replay_version_after: int


class BatchResult(BaseModel):
    accepted: int
    replay_version: int
    report_id: int
    late_detected: bool
