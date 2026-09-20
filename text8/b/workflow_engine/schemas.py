"""API 的 Pydantic 模型：模板定义、请求体、响应体。

定义 JSON 形态见 docs/api.md 与 demo/expense_demo.json。
"""

from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from workflow_engine.constants import ApprovalMode, TimeoutAction


# ---------------------------------------------------------------- 模板定义


class StartNode(BaseModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_\-]+$")
    type: Literal["start"]
    next: str = Field(min_length=1, max_length=100)


class EscalateAction(BaseModel):
    type: Literal[TimeoutAction.ESCALATE]
    to: list[str] = Field(min_length=1)


class AutoApproveAction(BaseModel):
    type: Literal[TimeoutAction.AUTO_APPROVE]


class AutoRejectAction(BaseModel):
    type: Literal[TimeoutAction.AUTO_REJECT]
    reason: str | None = None


TimeoutActionModel = Annotated[
    Union[EscalateAction, AutoApproveAction, AutoRejectAction],
    Field(discriminator="type"),
]


class ApprovalNode(BaseModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_\-]+$")
    type: Literal["approval"]
    name: str = Field(min_length=1, max_length=200)
    mode: Literal[ApprovalMode.ALL, ApprovalMode.ANY]
    approvers: list[str] = Field(min_length=1)
    next: str = Field(min_length=1, max_length=100)
    on_reject: str | None = Field(default=None, max_length=100)
    timeout_seconds: int | None = Field(default=None, ge=1, le=365 * 24 * 3600)
    timeout_action: TimeoutActionModel | None = None


class ConditionBranch(BaseModel):
    when: str = Field(min_length=1, max_length=1000)
    next: str = Field(min_length=1, max_length=100)
    name: str | None = Field(default=None, max_length=200)


class ConditionNode(BaseModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_\-]+$")
    type: Literal["condition"]
    branches: list[ConditionBranch] = Field(min_length=1)
    default: str | None = Field(default=None, max_length=100)


class EndNode(BaseModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_\-]+$")
    type: Literal["end"]
    outcome: Literal["approved", "rejected"] = "approved"
    name: str | None = Field(default=None, max_length=200)


Node = Annotated[
    Union[StartNode, ApprovalNode, ConditionNode, EndNode],
    Field(discriminator="type"),
]


class TemplateDefinition(BaseModel):
    model_config = {"extra": "forbid"}

    key: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_\-]+$")
    name: str = Field(min_length=1, max_length=200)
    nodes: list[Node] = Field(min_length=2)


# ---------------------------------------------------------------- 模板接口


class TemplateSummary(BaseModel):
    id: int
    key: str
    name: str
    current_version_id: int | None
    current_version_number: int | None
    created_at: datetime


class VersionOut(BaseModel):
    id: int
    template_id: int
    version: int
    status: str
    checksum: str
    published_at: datetime | None
    created_at: datetime


class VersionDetail(VersionOut):
    definition: dict


class CreateTemplateRequest(BaseModel):
    definition: TemplateDefinition
    publish: bool = True


class CreateVersionRequest(BaseModel):
    definition: TemplateDefinition
    publish: bool = True


class PublishRequest(BaseModel):
    pass


# ---------------------------------------------------------------- 实例接口


class StartInstanceRequest(BaseModel):
    template_key: str = Field(min_length=1, max_length=100)
    version: int | None = None
    title: str = Field(min_length=1, max_length=200)
    business_key: str | None = Field(default=None, max_length=200)
    context: dict = Field(default_factory=dict)


class TaskOut(BaseModel):
    id: int
    node_id: str
    assignee: str
    status: str
    decided_at: datetime | None
    comment: str | None


class AuditOut(BaseModel):
    id: int
    event_type: str
    node_id: str | None
    actor: str | None
    actor_type: str
    detail: dict
    request_id: str | None
    created_at: datetime


class InstanceOut(BaseModel):
    id: int
    template_id: int
    version_id: int
    version_number: int
    business_key: str | None
    title: str
    submitter: str
    status: str
    current_node_id: str | None
    context: dict
    reject_reason: str | None
    completed_at: datetime | None
    created_at: datetime


class InstanceDetail(InstanceOut):
    pending_tasks: list[TaskOut]
    history: list[AuditOut]


class DecisionRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=100)
    expected_version: int
    action: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=2000)


class WithdrawRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=100)
    expected_version: int
    comment: str | None = Field(default=None, max_length=2000)


class DecisionResult(BaseModel):
    status: str
    current_node_id: str | None
    advanced: bool
    request_id: str
    idempotent_replay: bool = False


class WithdrawResult(BaseModel):
    instance_id: int
    status: str
    request_id: str
    idempotent_replay: bool = False


class Message(BaseModel):
    message: str
