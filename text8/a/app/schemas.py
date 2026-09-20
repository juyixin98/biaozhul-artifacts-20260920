"""请求/响应模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TemplateCreate(BaseModel):
    key: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    name: str = Field(min_length=1, max_length=128)
    definition: dict[str, Any]


class VersionCreate(BaseModel):
    definition: dict[str, Any]


class InstanceStart(BaseModel):
    template_key: str = Field(min_length=1, max_length=64)
    submitter: str = Field(min_length=1, max_length=64)
    context: dict[str, Any] = Field(default_factory=dict)
    request_id: str = Field(min_length=1, max_length=64)


class TaskDecision(BaseModel):
    decision: str = Field(pattern=r"^(approve|reject)$")
    actor: str = Field(min_length=1, max_length=64)
    # 预期版本：调用方基于查询到的 template_version 操作，防止按错版本处理。
    expected_version: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=64)
    comment: str | None = Field(default=None, max_length=2000)


class WithdrawRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=1, max_length=64)


class PendingTask(BaseModel):
    task_id: int
    node_id: str
    assignee: str


class InstanceView(BaseModel):
    instance_id: str
    status: str
    template_version: int
    submitter: str
    current_node_id: str | None
    reject_reason: str | None
    pending_tasks: list[PendingTask]


class HistoryItem(BaseModel):
    id: int
    node_id: str | None
    event_type: str
    actor: str | None
    detail: dict[str, Any]
    created_at: str


class VersionView(BaseModel):
    version: int
    status: str
    created_at: str
    published_at: str | None


class TemplateView(BaseModel):
    key: str
    name: str
    current_version: int | None
    versions: list[VersionView]
