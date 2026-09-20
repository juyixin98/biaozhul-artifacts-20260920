"""Pydantic schemas: process definitions and API request/response bodies."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Process definition (JSON DSL)
# --------------------------------------------------------------------------- #

NodeType = Literal["start", "approval", "condition", "end"]
ApprovalMode = Literal["all", "any"]


class NodeDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    type: NodeType
    name: str | None = None
    # approval node
    mode: ApprovalMode | None = None
    assignees: list[str] | None = None
    timeout_seconds: int | None = Field(default=None, ge=1)
    escalate_to: list[str] | None = None
    # end node
    terminal: Literal["approved", "rejected"] | None = None


class EdgeDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    # condition edges leaving a condition node; absent on a default edge.
    expression: str | None = None


class Definition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[NodeDef] = Field(min_length=2)
    edges: list[EdgeDef] = Field(min_length=1)


# --------------------------------------------------------------------------- #
# Template API
# --------------------------------------------------------------------------- #


class TemplateCreate(BaseModel):
    key: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.\-]+$")
    name: str = Field(min_length=1, max_length=255)
    definition: Definition


class TemplateVersionCreate(BaseModel):
    definition: Definition


class VersionOut(BaseModel):
    version: int
    status: str
    created_at: datetime
    published_at: datetime | None


class TemplateOut(BaseModel):
    id: uuid.UUID
    key: str
    name: str
    current_version: int | None
    created_at: datetime
    versions: list[VersionOut]


# --------------------------------------------------------------------------- #
# Instance API
# --------------------------------------------------------------------------- #


class InstanceStart(BaseModel):
    template_key: str
    version: int | None = None  # default = current published
    submitter: str = Field(min_length=1, max_length=128)
    business_key: str | None = Field(default=None, max_length=255)
    payload: dict[str, Any] = Field(default_factory=dict)


class TaskOut(BaseModel):
    id: uuid.UUID
    node_id: str
    node_name: str | None
    generation: int
    assignee: str
    status: str
    created_at: datetime
    closed_at: datetime | None


class InstanceOut(BaseModel):
    id: uuid.UUID
    template_key: str
    version: int
    status: str
    submitter: str
    business_key: str | None
    current_node_id: str | None
    current_node_name: str | None
    reject_reason: str | None
    payload: dict[str, Any]
    created_at: datetime
    finished_at: datetime | None
    open_tasks: list[TaskOut]


# --------------------------------------------------------------------------- #
# Approval / withdraw API
# --------------------------------------------------------------------------- #


class DecisionIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    instance_id: uuid.UUID
    expected_version: int = Field(ge=1)
    actor: str = Field(min_length=1, max_length=128)
    decision: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=2000)


class WithdrawIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    instance_id: uuid.UUID
    expected_version: int | None = None
    submitter: str = Field(min_length=1, max_length=128)


class HistoryOut(BaseModel):
    seq: int
    action: str
    node_id: str | None
    actor: str | None
    detail: dict[str, Any]
    comment: str | None
    created_at: datetime
