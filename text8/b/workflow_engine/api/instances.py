"""流程实例接口：发起、查询、审批、撤回、历史。

身份：演示项目使用请求头 ``X-User`` 标识当前用户（不做签名/登录）。
所有"只能操作自己的待办/只有提交人可撤回"的鉴权都基于这个身份在服务端完成，
客户端传入的实例 ID / 任务 ID 无法越权。
"""

from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy.orm import Session

from workflow_engine.api.dependencies import run_idempotent
from workflow_engine.database import get_session
from workflow_engine.engine import FlowEngine
from workflow_engine.schemas import (
    AuditOut,
    DecisionRequest,
    DecisionResult,
    InstanceDetail,
    InstanceOut,
    StartInstanceRequest,
    WithdrawRequest,
    WithdrawResult,
)
from workflow_engine.services import instances as instance_service
from workflow_engine.services.templates import get_instance_definition

router = APIRouter(tags=["instances"])


@router.post("/instances", response_model=InstanceDetail, status_code=status.HTTP_201_CREATED)
def start_instance(
    body: StartInstanceRequest,
    session: Session = Depends(get_session),
    x_user: str = Header(..., alias="X-User", min_length=1, max_length=100),
):
    with session.begin():
        instance = instance_service.start_instance(
            session,
            template_key=body.template_key,
            version=body.version,
            title=body.title,
            submitter=x_user,
            business_key=body.business_key,
            context=body.context,
        )
        session.flush()  # 确保 activity/tasks 落库后再组装视图
        detail = instance_service.build_detail(session, instance)
    return detail


@router.get("/instances/{instance_id}", response_model=InstanceDetail)
def get_instance(instance_id: int, session: Session = Depends(get_session)):
    instance = instance_service.get_instance(session, instance_id)
    return instance_service.build_detail(session, instance)


@router.get("/instances/{instance_id}/history", response_model=list[AuditOut])
def get_history(instance_id: int, session: Session = Depends(get_session)):
    return instance_service.build_detail(session, instance_service.get_instance(session, instance_id))[
        "history"
    ]


@router.post("/instances/{instance_id}/decide", response_model=DecisionResult)
def decide(
    instance_id: int,
    body: DecisionRequest,
    session: Session = Depends(get_session),
    x_user: str = Header(..., alias="X-User", min_length=1, max_length=100),
    task_id: int | None = Query(default=None),
    node_id: str | None = Query(default=None),
):
    """提交审批决定。

    - 通过 query 参数 ``task_id`` 或 ``node_id`` 指定待办；
    - 必须携带 request_id（幂等）与 expected_version（乐观版本校验）；
    - 全签：所有人通过才推进，任一人拒绝即拒绝并关闭其余待办；
    - 任签：任一人通过即推进并关闭其余待办。
    """

    def execute() -> dict:
        instance = instance_service.get_instance(session, instance_id)
        instance_service.check_expected_version(instance, body.expected_version)
        definition = get_instance_definition(session, instance)
        result = FlowEngine(session).decide(
            instance_id=instance_id,
            definition=definition,
            actor=x_user,
            action=body.action,
            task_id=task_id,
            node_id=node_id,
            comment=body.comment,
            request_id=body.request_id,
        )
        return result

    payload, replayed = run_idempotent(session, body.request_id, execute)
    payload["idempotent_replay"] = replayed
    return payload


@router.post("/instances/{instance_id}/withdraw", response_model=WithdrawResult)
def withdraw(
    instance_id: int,
    body: WithdrawRequest,
    session: Session = Depends(get_session),
    x_user: str = Header(..., alias="X-User", min_length=1, max_length=100),
):
    """提交人在流程结束前撤回。"""

    def execute() -> dict:
        instance = instance_service.get_instance(session, instance_id)
        instance_service.check_expected_version(instance, body.expected_version)
        definition = get_instance_definition(session, instance)
        return FlowEngine(session).withdraw(
            instance_id=instance_id,
            definition=definition,
            actor=x_user,
            comment=body.comment,
            request_id=body.request_id,
        )

    payload, replayed = run_idempotent(session, body.request_id, execute)
    payload["idempotent_replay"] = replayed
    return payload
