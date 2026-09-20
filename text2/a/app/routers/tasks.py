from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.clock import Clock, get_clock
from app.config import settings
from app.database import get_db
from app.errors import ConflictError, NotFoundError
from app.models import AdjustmentLog, Assignment, CareTask
from app.schemas import (AdjustmentLogOut, AdjustBody, AssignmentOut,
                         OfferCreate, OfferOut, TaskOut)
from app.services import assignment as svc

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _get_task(db: Session, task_id: int) -> CareTask:
    task = db.get(CareTask, task_id)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    return task


@router.get("", response_model=list[TaskOut])
def list_tasks(status: str | None = None, unit_id: str | None = None,
               plan_id: int | None = None, caregiver_id: int | None = None,
               db: Session = Depends(get_db)):
    q = db.query(CareTask)
    if status:
        q = q.filter(CareTask.status == status)
    if unit_id:
        q = q.filter(CareTask.unit_id == unit_id)
    if plan_id:
        q = q.filter(CareTask.plan_id == plan_id)
    if caregiver_id is not None:
        q = q.join(Assignment, Assignment.task_id == CareTask.id).filter(
            Assignment.caregiver_id == caregiver_id)
    return q.order_by(CareTask.scheduled_start, CareTask.id).all()


@router.get("/{task_id}", response_model=TaskOut)
def get_task(task_id: int, db: Session = Depends(get_db)):
    return _get_task(db, task_id)


@router.get("/{task_id}/history", response_model=list[AdjustmentLogOut])
def task_history(task_id: int, db: Session = Depends(get_db)):
    _get_task(db, task_id)
    return (db.query(AdjustmentLog)
            .filter(AdjustmentLog.task_id == task_id)
            .order_by(AdjustmentLog.id).all())


@router.post("/{task_id}/offer", response_model=OfferOut, status_code=201)
def offer_task(task_id: int, body: OfferCreate, db: Session = Depends(get_db),
               clock: Clock = Depends(get_clock)):
    """向最优（或指定）候选人发出邀请。无人满足约束时返回 422 及具体原因。"""
    task = _get_task(db, task_id)
    offer = svc.create_offer(db, task, clock.now(), settings,
                             caregiver_id=body.caregiver_id)
    db.commit()
    db.refresh(offer)
    return offer


@router.post("/{task_id}/adjust", response_model=AssignmentOut)
def adjust_task(task_id: int, body: AdjustBody, db: Session = Depends(get_db),
                clock: Clock = Depends(get_clock)):
    """人工调整：校验协调员授权单元，走同一套约束检查，记录原因与历史。"""
    task = _get_task(db, task_id)
    assignment = svc.manual_adjust(
        db, task=task, coordinator_id=body.coordinator_id,
        caregiver_id=body.caregiver_id, reason=body.reason,
        now=clock.now(), settings=settings)
    db.commit()
    db.refresh(assignment)
    return assignment


@router.post("/{task_id}/complete", response_model=TaskOut)
def complete_task(task_id: int, db: Session = Depends(get_db)):
    task = _get_task(db, task_id)
    if task.status != "assigned":
        raise ConflictError(f"task {task_id} is {task.status}, cannot complete")
    task.status = "completed"
    db.commit()
    db.refresh(task)
    return task
