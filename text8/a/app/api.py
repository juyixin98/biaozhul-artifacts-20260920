"""HTTP API。

幂等约定：发起类请求（启动/审批/撤回）必须携带客户端生成的 request_id；
重复请求（相同 request_id + 相同参数）返回原结果；相同 request_id 不同参数
返回 409 且不写入业务数据。
"""
from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse

from app import engine
from app.db import SessionLocal
from app.errors import FlowError
from app.models import Instance, Task, TemplateVersion
from app.schemas import (
    HistoryItem,
    InstanceStart,
    InstanceView,
    PendingTask,
    TaskDecision,
    TemplateCreate,
    TemplateView,
    VersionCreate,
    VersionView,
    WithdrawRequest,
)

app = FastAPI(
    title="流程编排引擎",
    version="1.0.0",
    description="基于 JSON DSL 的审批流引擎：版本不可变、版本隔离、"
    "全签/任签、条件分支、撤回、超时升级、幂等与并发安全。",
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.exception_handler(FlowError)
def flow_error_handler(request, exc: FlowError):
    body: dict = {"error": exc.message}
    if exc.details:
        body["details"] = exc.details
    return JSONResponse(status_code=exc.status_code, content=body)


class RollbackBody(BaseModel):
    version: int = Field(ge=1)


# ---------------------------------------------------------------- 模板管理


@app.post("/templates", response_model=TemplateView, status_code=201, tags=["模板"])
def create_template(body: TemplateCreate, db: Session = Depends(get_db)):
    engine.create_template(db, body.key, body.name, body.definition)
    return _template_view(db, body.key)


@app.get("/templates/{key}", response_model=TemplateView, tags=["模板"])
def get_template(key: str, db: Session = Depends(get_db)):
    return _template_view(db, key)


@app.post(
    "/templates/{key}/versions",
    response_model=TemplateView,
    status_code=201,
    tags=["模板"],
)
def add_version(key: str, body: VersionCreate, db: Session = Depends(get_db)):
    tpl = engine._get_template(db, key)
    engine.add_version(db, tpl, body.definition)
    return _template_view(db, key)


@app.post(
    "/templates/{key}/versions/{version}/publish",
    response_model=TemplateView,
    tags=["模板"],
)
def publish(key: str, version: int, db: Session = Depends(get_db)):
    engine.publish_version(db, key, version)
    return _template_view(db, key)


@app.post("/templates/{key}/rollback", response_model=TemplateView, tags=["模板"])
def rollback(key: str, body: RollbackBody, db: Session = Depends(get_db)):
    engine.rollback_template(db, key, body.version)
    return _template_view(db, key)


# ---------------------------------------------------------------- 实例


@app.post("/instances", response_model=InstanceView, status_code=201, tags=["实例"])
def start_instance(body: InstanceStart, db: Session = Depends(get_db)):
    _, replay = engine.start_instance(
        db,
        template_key=body.template_key,
        submitter=body.submitter,
        context=body.context,
        request_id=body.request_id,
    )
    if replay is not None:
        # 幂等重放：返回首次的处理结果（200，而非再次 201）。
        return JSONResponse(status_code=200, content=replay)
    instance = db.scalar(
        select(Instance).where(Instance.start_request_id == body.request_id)
    )
    return _to_instance_view(instance, db)


# ---------------------------------------------------------------- 审批操作


@app.post(
    "/instances/{instance_id}/tasks/{task_id}/decision",
    response_model=InstanceView,
    tags=["审批"],
)
def decide(instance_id: str, task_id: int, body: TaskDecision, db: Session = Depends(get_db)):
    inst, replay = engine.decide_task(
        db,
        instance_id=instance_id,
        task_id=task_id,
        request_id=body.request_id,
        expected_version=body.expected_version,
        actor=body.actor,
        decision=body.decision,
        comment=body.comment,
    )
    if replay is not None:
        return JSONResponse(status_code=200, content=replay)
    return _to_instance_view(inst, db)


@app.post(
    "/instances/{instance_id}/withdraw",
    response_model=InstanceView,
    tags=["审批"],
)
def withdraw_endpoint(
    instance_id: str, body: WithdrawRequest, db: Session = Depends(get_db)
):
    inst, replay = engine.withdraw(
        db,
        instance_id=instance_id,
        request_id=body.request_id,
        actor=body.actor,
    )
    if replay is not None:
        return JSONResponse(status_code=200, content=replay)
    return _to_instance_view(inst, db)


# ---------------------------------------------------------------- 查询


@app.get("/instances/{instance_id}", response_model=InstanceView, tags=["查询"])
def instance_detail(instance_id: str, db: Session = Depends(get_db)):
    instance = engine.get_instance(db, instance_id)
    return _to_instance_view(instance, db)


@app.get(
    "/instances/{instance_id}/history",
    response_model=list[HistoryItem],
    tags=["查询"],
)
def history(instance_id: str, db: Session = Depends(get_db)):
    events = engine.get_history(db, instance_id)
    return [
        HistoryItem(
            id=e.id,
            node_id=e.node_id,
            event_type=e.event_type,
            actor=e.actor,
            detail=e.detail,
            created_at=e.created_at.isoformat(),
        )
        for e in events
    ]


@app.get("/instances/{instance_id}/tasks", tags=["查询"])
def list_tasks(
    instance_id: str,
    status_filter: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
):
    """返回当前位置、全部待办及其状态。"""
    instance = engine.get_instance(db, instance_id)
    q = select(Task).where(Task.instance_id == instance.id)
    if status_filter:
        q = q.where(Task.status == status_filter)
    rows = db.scalars(q.order_by(Task.id)).all()
    return {
        "instance_id": instance.id,
        "current_node_id": instance.current_node_id,
        "tasks": [
            {
                "task_id": t.id,
                "node_id": t.node_id,
                "assignee": t.assignee,
                "status": t.status,
                "handled_by": t.handled_by,
                "comment": t.comment,
                "handled_at": t.handled_at.isoformat() if t.handled_at else None,
            }
            for t in rows
        ],
    }


@app.get("/health", tags=["运维"])
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- 序列化


def _to_instance_view(instance: Instance, db: Session) -> InstanceView:
    pending = db.scalars(
        select(Task)
        .where(Task.instance_id == instance.id, Task.status == "pending")
        .order_by(Task.id)
    ).all()
    return InstanceView(
        instance_id=instance.id,
        status=instance.status,
        template_version=instance.version_number,
        submitter=instance.submitter,
        current_node_id=instance.current_node_id,
        reject_reason=instance.reject_reason,
        pending_tasks=[
            PendingTask(task_id=t.id, node_id=t.node_id, assignee=t.assignee)
            for t in pending
        ],
    )


def _template_view(db: Session, key: str) -> TemplateView:
    tpl = engine._get_template(db, key)
    versions = db.scalars(
        select(TemplateVersion)
        .where(TemplateVersion.template_id == tpl.id)
        .order_by(TemplateVersion.version)
    ).all()
    current_number = None
    if tpl.current_version_id:
        for v in versions:
            if v.id == tpl.current_version_id:
                current_number = v.version
    return TemplateView(
        key=tpl.key,
        name=tpl.name,
        current_version=current_number,
        versions=[
            VersionView(
                version=v.version,
                status=v.status,
                created_at=v.created_at.isoformat(),
                published_at=v.published_at.isoformat() if v.published_at else None,
            )
            for v in versions
        ],
    )
