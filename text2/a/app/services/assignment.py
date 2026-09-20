"""约束分配：资格、重叠、休息、周工时校验；候选人排序；邀请/接受/超时重排；人工调整。"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import (ConflictError, ConstraintViolationError,
                        ForbiddenError, NotFoundError)
from app.models import (AdjustmentLog, Assignment, Caregiver, CareTask,
                        Coordinator, Offer)


# ---------------------------------------------------------------- 工时工具

def iso_week(dt: datetime) -> tuple[int, int]:
    iso = dt.isocalendar()
    return (iso[0], iso[1])


def split_by_week(start: datetime, end: datetime) -> list[tuple[tuple[int, int], float]]:
    """把跨日/跨周班次按实际时间拆分到对应 ISO 周，返回 [(（年， 周）, 小时)]。"""
    parts: list[tuple[tuple[int, int], float]] = []
    cur = start
    while cur < end:
        days = (7 - cur.weekday()) % 7
        if days == 0:
            days = 7
        next_monday = (cur + timedelta(days=days)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        seg_end = min(next_monday, end)
        parts.append((iso_week(cur), (seg_end - cur).total_seconds() / 3600.0))
        cur = seg_end
    return parts


def week_bounds(year: int, week: int) -> tuple[datetime, datetime]:
    monday = datetime.fromisocalendar(year, week, 1).replace(tzinfo=timezone.utc)
    return monday, monday + timedelta(days=7)


def assigned_tasks(session: Session, caregiver_id: int,
                   exclude_task_id: int | None = None) -> list[CareTask]:
    q = (
        session.query(CareTask)
        .join(Assignment, Assignment.task_id == CareTask.id)
        .filter(Assignment.caregiver_id == caregiver_id,
                CareTask.status == "assigned")
    )
    if exclude_task_id is not None:
        q = q.filter(CareTask.id != exclude_task_id)
    return q.all()


def weekly_hours(session: Session, caregiver_id: int, year: int, week: int,
                 exclude_task_id: int | None = None) -> float:
    start, end = week_bounds(year, week)
    total = 0.0
    for t in assigned_tasks(session, caregiver_id, exclude_task_id):
        if t.scheduled_start < end and t.scheduled_end > start:
            for (y, w), hours in split_by_week(t.scheduled_start, t.scheduled_end):
                if (y, w) == (year, week):
                    total += hours
    return total


def load_hours(session: Session, caregiver_id: int, now: datetime,
               horizon_days: int = 14) -> float:
    """既有负载：未来 horizon 内已分配工时。"""
    end = now + timedelta(days=horizon_days)
    total = 0.0
    for t in assigned_tasks(session, caregiver_id):
        s, e = max(t.scheduled_start, now), min(t.scheduled_end, end)
        if s < e:
            total += (e - s).total_seconds() / 3600.0
    return total


# ---------------------------------------------------------------- 约束校验

def validate_assignment(session: Session, caregiver: Caregiver, task: CareTask,
                        settings: Settings) -> list[str]:
    """人工调整与自动分配共用的同一套约束检查。返回违规原因列表（空 = 通过）。"""
    violations: list[str] = []
    start, end = task.scheduled_start, task.scheduled_end

    # 1. 资格须覆盖整个任务时段
    covering = [
        q for q in caregiver.qualifications
        if q.code == task.required_qualification
        and q.valid_from <= start and q.valid_until >= end
    ]
    if not covering:
        violations.append(
            f"qualification '{task.required_qualification}' does not cover "
            f"{start.isoformat()}..{end.isoformat()}"
        )

    # 2. 时间不重叠；3. 班次间至少休息 min_rest_hours
    min_rest = timedelta(hours=settings.min_rest_hours)
    for other in assigned_tasks(session, caregiver.id, exclude_task_id=task.id):
        if other.scheduled_start < end and start < other.scheduled_end:
            violations.append(
                f"overlaps task {other.id} "
                f"({other.scheduled_start.isoformat()}..{other.scheduled_end.isoformat()})"
            )
            continue
        if start >= other.scheduled_end:
            gap = start - other.scheduled_end
        else:
            gap = other.scheduled_start - end
        if gap < min_rest:
            violations.append(
                f"rest gap {gap.total_seconds() / 3600:.1f}h to task {other.id} "
                f"< required {settings.min_rest_hours}h"
            )

    # 4. 周工时上限（跨日班次按实际时间拆分到对应周）
    for (year, week), hours in split_by_week(start, end):
        existing = weekly_hours(session, caregiver.id, year, week,
                                exclude_task_id=task.id)
        if existing + hours > settings.weekly_hour_limit + 1e-9:
            violations.append(
                f"weekly hours {existing + hours:.2f} exceed "
                f"{settings.weekly_hour_limit} in ISO week {year}-W{week:02d}"
            )

    # 5. 前置任务须已完成
    if task.depends_on_task_id:
        dep = session.get(CareTask, task.depends_on_task_id)
        if dep is not None and dep.status != "completed":
            violations.append(
                f"prerequisite task {dep.id} not completed (status={dep.status})"
            )

    return violations


# ---------------------------------------------------------------- 候选人排序

def rank_candidates(session: Session, task: CareTask, now: datetime,
                    settings: Settings) -> tuple[list, list]:
    """返回 (合格候选人[(caregiver, remaining, load)], 被拒者[{caregiver_id, reasons}])。
    排序：剩余可用时间降序 -> 既有负载升序 -> ID 升序（稳定）。"""
    declined = {o.caregiver_id for o in task.offers
                if o.status in ("expired", "rejected")}
    caregivers = (session.query(Caregiver)
                  .filter(Caregiver.unit_id == task.unit_id).all())

    eligible, rejected = [], []
    for cg in caregivers:
        if cg.id in declined:
            continue
        violations = validate_assignment(session, cg, task, settings)
        if violations:
            rejected.append({"caregiver_id": cg.id, "reasons": violations})
            continue
        remaining = min(
            settings.weekly_hour_limit
            - weekly_hours(session, cg.id, year, week, exclude_task_id=task.id)
            for (year, week), _ in split_by_week(task.scheduled_start, task.scheduled_end)
        )
        eligible.append((cg, remaining, load_hours(session, cg.id, now)))

    eligible.sort(key=lambda e: (-e[1], e[2], e[0].id))
    return eligible, rejected


# ---------------------------------------------------------------- 邀请 / 接受

def create_offer(session: Session, task: CareTask, now: datetime,
                 settings: Settings, caregiver_id: int | None = None) -> Offer:
    if task.status not in ("pending", "offered"):
        raise ConflictError(f"task {task.id} is {task.status}, cannot offer")

    # 同一任务同一时刻只保留一个待响应邀请
    for o in task.offers:
        if o.status == "pending":
            o.status = "superseded"

    if caregiver_id is not None:
        caregiver = session.get(Caregiver, caregiver_id)
        if caregiver is None:
            raise NotFoundError(f"caregiver {caregiver_id} not found")
        violations = validate_assignment(session, caregiver, task, settings)
        if violations:
            raise ConstraintViolationError(
                "caregiver not eligible",
                [{"caregiver_id": caregiver.id, "reasons": violations}],
            )
    else:
        eligible, rejected = rank_candidates(session, task, now, settings)
        if not eligible:
            raise ConstraintViolationError("no eligible caregiver", rejected)
        caregiver = eligible[0][0]

    offer = Offer(
        task_id=task.id,
        caregiver_id=caregiver.id,
        status="pending",
        offered_at=now,
        expires_at=now + timedelta(minutes=settings.offer_ttl_minutes),
    )
    session.add(offer)
    task.status = "offered"
    session.add(AdjustmentLog(
        task_id=task.id, actor="system", action="offer_created",
        reason=None, details={"offer_caregiver_id": caregiver.id}, created_at=now,
    ))
    session.flush()
    return offer


def accept_offer(session: Session, offer_id: int, caregiver_id: int,
                 now: datetime) -> tuple[Offer, Assignment | None]:
    offer = session.get(Offer, offer_id)
    if offer is None:
        raise NotFoundError(f"offer {offer_id} not found")
    if offer.caregiver_id != caregiver_id:
        raise ForbiddenError("offer belongs to another caregiver")

    # 重复请求幂等：已接受的邀请再次接受，返回既有分配，不重复占工时
    if offer.status == "accepted":
        assignment = (session.query(Assignment)
                      .filter_by(task_id=offer.task_id).one_or_none())
        return offer, assignment
    if offer.status != "pending":
        raise ConflictError(f"offer is {offer.status}")

    # 原子接受：并发/超时竞争下只有一个请求能把 pending 且未过期的邀请置为 accepted
    res = session.execute(
        update(Offer)
        .where(Offer.id == offer_id, Offer.status == "pending",
               Offer.expires_at > now)
        .values(status="accepted", responded_at=now)
    )
    if res.rowcount == 0:
        raise ConflictError("offer expired or already handled")

    # 原子占用任务：多人并发接受时只有一人成功（唯一约束兜底）
    res = session.execute(
        update(CareTask)
        .where(CareTask.id == offer.task_id,
               CareTask.status.in_(["pending", "offered"]))
        .values(status="assigned")
    )
    if res.rowcount == 0:
        raise ConflictError("task already assigned")

    session.execute(
        update(Offer)
        .where(Offer.task_id == offer.task_id, Offer.status == "pending")
        .values(status="superseded")
    )

    assignment = Assignment(
        task_id=offer.task_id, caregiver_id=caregiver_id,
        source="offer", created_by="system", created_at=now,
    )
    session.add(assignment)
    session.add(AdjustmentLog(
        task_id=offer.task_id, actor="system", action="offer_accepted",
        reason=None, details={"offer_id": offer_id, "caregiver_id": caregiver_id},
        created_at=now,
    ))
    session.flush()
    return offer, assignment


def reject_offer(session: Session, offer_id: int, caregiver_id: int,
                 now: datetime, settings: Settings) -> tuple[Offer, Offer | None]:
    offer = session.get(Offer, offer_id)
    if offer is None:
        raise NotFoundError(f"offer {offer_id} not found")
    if offer.caregiver_id != caregiver_id:
        raise ForbiddenError("offer belongs to another caregiver")
    if offer.status != "pending":
        raise ConflictError(f"offer is {offer.status}")
    offer.status = "rejected"
    offer.responded_at = now

    task = offer.task
    task.status = "pending"
    session.flush()
    new_offer = _try_reoffer(session, task, now, settings)
    return offer, new_offer


def expire_offers(session: Session, now: datetime,
                  settings: Settings) -> list[dict]:
    """超时重排：失效所有过期邀请并尝试重新分配。"""
    stale = (session.query(Offer)
             .filter(Offer.status == "pending", Offer.expires_at <= now).all())
    results = []
    for offer in stale:
        offer.status = "expired"
        offer.responded_at = now
        task = offer.task
        session.add(AdjustmentLog(
            task_id=task.id, actor="system", action="offer_expired",
            reason="no response within TTL",
            details={"offer_id": offer.id, "caregiver_id": offer.caregiver_id},
            created_at=now,
        ))
        entry = {"expired_offer_id": offer.id, "task_id": task.id,
                 "new_offer_id": None}
        if task.status == "offered":
            task.status = "pending"
            session.flush()
            new_offer = _try_reoffer(session, task, now, settings)
            if new_offer is not None:
                entry["new_offer_id"] = new_offer.id
        results.append(entry)
    return results


def _try_reoffer(session: Session, task: CareTask, now: datetime,
                 settings: Settings) -> Offer | None:
    try:
        return create_offer(session, task, now, settings)
    except ConstraintViolationError:
        return None  # 无人可排：保持 pending，由调用方查看约束详情


# ---------------------------------------------------------------- 人工调整

def manual_adjust(session: Session, *, task: CareTask,
                  coordinator_id: int, caregiver_id: int, reason: str,
                  now: datetime, settings: Settings) -> Assignment:
    coordinator = session.get(Coordinator, coordinator_id)
    if coordinator is None:
        raise NotFoundError(f"coordinator {coordinator_id} not found")
    authorized = {u.unit_id for u in coordinator.units}
    if task.unit_id not in authorized:
        raise ForbiddenError(
            f"coordinator {coordinator_id} is not authorized for unit {task.unit_id}"
        )
    if task.status in ("completed", "cancelled"):
        raise ConflictError(f"task {task.id} is {task.status}, cannot adjust")

    caregiver = session.get(Caregiver, caregiver_id)
    if caregiver is None:
        raise NotFoundError(f"caregiver {caregiver_id} not found")

    # 与自动分配完全相同的约束检查
    violations = validate_assignment(session, caregiver, task, settings)
    if violations:
        raise ConstraintViolationError(
            "manual adjustment violates constraints",
            [{"caregiver_id": caregiver.id, "reasons": violations}],
        )

    for o in task.offers:
        if o.status == "pending":
            o.status = "superseded"

    assignment = (session.query(Assignment)
                  .filter_by(task_id=task.id).one_or_none())
    old_caregiver_id = assignment.caregiver_id if assignment else None
    if assignment is None:
        assignment = Assignment(
            task_id=task.id, caregiver_id=caregiver.id, source="manual",
            created_by=f"coordinator:{coordinator_id}", created_at=now,
        )
        session.add(assignment)
    else:
        assignment.caregiver_id = caregiver.id
        assignment.source = "manual"
        assignment.created_by = f"coordinator:{coordinator_id}"
        assignment.created_at = now
    task.status = "assigned"

    session.add(AdjustmentLog(
        task_id=task.id, actor=f"coordinator:{coordinator_id}",
        action="manual_adjust", reason=reason,
        details={"old_caregiver_id": old_caregiver_id,
                 "new_caregiver_id": caregiver.id},
        created_at=now,
    ))
    session.flush()
    return assignment
