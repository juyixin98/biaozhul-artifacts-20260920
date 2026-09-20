"""Task generation and inspection endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Actor, get_current_coordinator
from app.config import settings
from app.db import get_db
from app.deps import get_clock, serialize_task
from app.enums import ACTIVE_TASK_STATUSES, TaskStatus
from app.models import Task
from app.schemas import GenerateResponse, TaskOut
from app.services.generation import generate_tasks

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.post("/generate/{plan_id}", response_model=GenerateResponse)
def generate_for_plan(
    plan_id: int,
    request: Request,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_coordinator),
) -> dict:
    """(Re)generate the next 14 days for a plan.

    Idempotent: calling it repeatedly never creates duplicate orders — only
    missing occurrences are added.
    """
    actor.ensure_can_access_plan(db, plan_id)
    tasks = generate_tasks(
        db, plan_id=plan_id, now=get_clock(request).now()
    )
    db.commit()
    return {
        "plan_id": plan_id,
        "horizon_days": settings.horizon_days,
        "tasks": [serialize_task(t) for t in tasks],
    }


@router.get("", response_model=list[TaskOut])
def list_tasks(
    plan_id: int | None = None,
    unit_id: int | None = None,
    status: TaskStatus | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_coordinator),
) -> list[dict]:
    stmt = select(Task).order_by(Task.starts_at).limit(limit)
    out: list[dict] = []
    for task in db.scalars(stmt):
        if task.plan.unit_id not in actor.unit_ids():
            continue
        if plan_id is not None and task.plan_id != plan_id:
            continue
        if unit_id is not None and task.plan.unit_id != unit_id:
            continue
        if status is not None and task.status != status.value:
            continue
        out.append(serialize_task(task))
    return out


@router.get("/{task_id}", response_model=TaskOut)
def get_task(
    task_id: int,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_coordinator),
) -> dict:
    actor.ensure_can_access_task(db, task_id)
    return serialize_task(db.get(Task, task_id))
