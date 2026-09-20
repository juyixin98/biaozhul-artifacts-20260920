from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---- templates -------------------------------------------------------------

class PublishTemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    definition: dict[str, Any]


class TemplateVersionOut(BaseModel):
    template_code: str
    version: int
    name: str
    definition: dict[str, Any]
    created_by: str
    created_at: datetime


# ---- instances -------------------------------------------------------------

class StartInstanceIn(BaseModel):
    template_code: str = Field(min_length=1, max_length=64)
    template_version: int | None = None
    business_key: str = Field(min_length=1, max_length=128)
    variables: dict[str, Any] = Field(default_factory=dict)
    submitter: str | None = Field(default=None, max_length=64)


class DecisionIn(BaseModel):
    action: str = Field(pattern="^(approve|reject)$")
    expected_version: int
    comment: str | None = None
    actor: str | None = Field(default=None, max_length=64)


class WithdrawIn(BaseModel):
    comment: str | None = None


class TaskOut(BaseModel):
    id: int
    node_id: str
    assignee: str
    status: str
    sign_strategy: str
    decided_by: str | None
    comment: str | None
    due_at: datetime | None
    escalated: bool
    created_at: datetime
    decided_at: datetime | None


class AuditOut(BaseModel):
    id: int
    instance_id: int | None
    event_type: str
    node_id: str | None
    actor: str | None
    detail: dict[str, Any]
    created_at: datetime


class InstanceOut(BaseModel):
    id: int
    template_code: str
    template_version: int
    business_key: str
    variables: dict[str, Any]
    status: str
    current_node_id: str | None
    submitter: str
    reject_reason: str | None
    created_at: datetime
    updated_at: datetime
    pending_tasks: list[TaskOut] = Field(default_factory=list)


class InstanceDetailOut(InstanceOut):
    history: list[AuditOut] = Field(default_factory=list)
