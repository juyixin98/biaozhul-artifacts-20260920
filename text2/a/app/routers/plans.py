from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.clock import Clock, get_clock
from app.config import settings
from app.database import get_db
from app.errors import NotFoundError
from app.models import CarePlan
from app.schemas import PlanCreate, PlanOut, PlanRevise
from app.services import scheduling

router = APIRouter(prefix="/plans", tags=["plans"])


def _get_plan(db: Session, plan_id: int) -> CarePlan:
    plan = db.get(CarePlan, plan_id)
    if plan is None:
        raise NotFoundError(f"plan {plan_id} not found")
    return plan


def _check_window(window_start, window_end, duration_minutes: int) -> None:
    from app.errors import ConstraintViolationError
    day = datetime(2000, 1, 1)
    start = datetime.combine(day, window_start)
    end = datetime.combine(day, window_end)
    if end <= start:
        end += timedelta(days=1)  # 跨夜窗口，如 22:00 -> 次日 02:00
    if end - start < timedelta(minutes=duration_minutes):
        raise ConstraintViolationError("duration does not fit inside the time window")


@router.post("", response_model=PlanOut, status_code=201)
def create_plan(body: PlanCreate, db: Session = Depends(get_db)):
    _check_window(body.window_start, body.window_end, body.duration_minutes)
    if body.prerequisite_plan_id is not None:
        _get_plan(db, body.prerequisite_plan_id)
    plan = CarePlan(**body.model_dump())
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


@router.get("/{plan_id}", response_model=PlanOut)
def get_plan(plan_id: int, db: Session = Depends(get_db)):
    return _get_plan(db, plan_id)


@router.post("/{plan_id}/generate")
def generate(plan_id: int, db: Session = Depends(get_db),
             clock: Clock = Depends(get_clock)):
    """生成未来 14 天任务；重复调用幂等，不会重复建单。"""
    plan = _get_plan(db, plan_id)
    created = scheduling.generate_tasks(db, plan, clock.now(),
                                        settings.generation_horizon_days)
    db.commit()
    return {"created": len(created), "task_ids": [t.id for t in created]}


@router.put("/{plan_id}")
def revise(plan_id: int, body: PlanRevise, db: Session = Depends(get_db),
           clock: Clock = Depends(get_clock)):
    """计划改版：版本 +1，只取消尚未开始的任务并按新版本重新生成。"""
    plan = _get_plan(db, plan_id)
    changes = body.model_dump(exclude_unset=True)
    window_start = changes.get("window_start", plan.window_start)
    window_end = changes.get("window_end", plan.window_end)
    duration = changes.get("duration_minutes", plan.duration_minutes)
    _check_window(window_start, window_end, duration)
    cancelled, created = scheduling.revise_plan(
        db, plan, changes, clock.now(), settings.generation_horizon_days)
    db.commit()
    return {
        "id": plan.id,
        "version": plan.version,
        "cancelled_task_ids": [t.id for t in cancelled],
        "created_task_ids": [t.id for t in created],
    }
