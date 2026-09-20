"""任务生成：按计划周期生成未来 N 天任务，幂等；改版只影响未开始任务。"""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AdjustmentLog, CarePlan, CareTask

WEEKDAY_IDX = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _matches(plan: CarePlan, d) -> bool:
    if d < plan.start_date:
        return False
    if plan.frequency == "daily":
        return (d - plan.start_date).days % plan.interval == 0
    # weekly
    days = plan.byweekday or [list(WEEKDAY_IDX)[plan.start_date.weekday()]]
    if d.weekday() not in {WEEKDAY_IDX[x] for x in days}:
        return False
    monday_d = d - timedelta(days=d.weekday())
    monday_s = plan.start_date - timedelta(days=plan.start_date.weekday())
    return ((monday_d - monday_s).days // 7) % plan.interval == 0


def _find_dependency(session: Session, plan: CarePlan, local_date, tz) -> CareTask | None:
    """前置任务：前置计划在同一天（按计划服务时区）生成的最新有效任务。"""
    if not plan.prerequisite_plan_id:
        return None
    day_start = datetime.combine(local_date, time.min, tzinfo=tz).astimezone(timezone.utc)
    day_end = day_start + timedelta(days=1)
    return (
        session.query(CareTask)
        .filter(
            CareTask.plan_id == plan.prerequisite_plan_id,
            CareTask.status != "cancelled",
            CareTask.scheduled_start >= day_start,
            CareTask.scheduled_start < day_end,
        )
        .order_by(CareTask.plan_version.desc())
        .first()
    )


def generate_tasks(session: Session, plan: CarePlan, now: datetime,
                   horizon_days: int) -> list[CareTask]:
    """生成 [now, now+horizon] 内的任务。重复调用不会重复建单
    （(plan_id, plan_version, scheduled_start) 唯一约束 + savepoint 跳过）。"""
    tz = ZoneInfo(plan.timezone)
    today = now.astimezone(tz).date()
    first = max(today, plan.start_date)
    last = today + timedelta(days=horizon_days)

    created: list[CareTask] = []
    d = first
    while d <= last:
        if _matches(plan, d):
            start_local = datetime.combine(d, plan.window_start, tzinfo=tz)
            start_utc = start_local.astimezone(timezone.utc)
            if start_utc >= now:
                end_utc = start_utc + timedelta(minutes=plan.duration_minutes)
                dep = _find_dependency(session, plan, d, tz)
                task = CareTask(
                    plan_id=plan.id,
                    plan_version=plan.version,
                    scheduled_start=start_utc,
                    scheduled_end=end_utc,
                    status="pending",
                    required_qualification=plan.required_qualification,
                    unit_id=plan.unit_id,
                    depends_on_task_id=dep.id if dep else None,
                )
                try:
                    with session.begin_nested():
                        session.add(task)
                        session.flush()
                    created.append(task)
                except IntegrityError:
                    pass  # 已存在，跳过 —— 幂等
        d += timedelta(days=1)
    return created


def revise_plan(session: Session, plan: CarePlan, changes: dict, now: datetime,
                horizon_days: int) -> tuple[list[CareTask], list[CareTask]]:
    """改版：版本号 +1，取消旧版本中尚未开始的任务，按新版本重新生成。
    已开始（scheduled_start <= now）或已分配/已完成的任务不受影响。"""
    for key, value in changes.items():
        setattr(plan, key, value)
    plan.version += 1

    future_unstarted = (
        session.query(CareTask)
        .filter(
            CareTask.plan_id == plan.id,
            CareTask.plan_version < plan.version,
            CareTask.scheduled_start > now,
            CareTask.status.in_(["pending", "offered"]),
        )
        .all()
    )
    for task in future_unstarted:
        for offer in task.offers:
            if offer.status == "pending":
                offer.status = "superseded"
        task.status = "cancelled"
        session.add(AdjustmentLog(
            task_id=task.id, actor="system", action="cancel_by_revision",
            reason=f"plan revised to version {plan.version}",
            details={"plan_version": task.plan_version}, created_at=now,
        ))
    session.flush()

    created = generate_tasks(session, plan, now, horizon_days)
    return future_unstarted, created
