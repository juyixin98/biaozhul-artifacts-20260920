"""Care-plan task generation.

* Generates occurrences for the next 14 service-local days.
* Idempotent: (plan revision, template, service-local date) is a natural key;
  regenerating the same plan never creates duplicate tasks.
* Plan revisions only affect tasks that have **not started**: started / in
  progress / completed occurrences stay on the old revision; every future
  occurrence is cancelled and regenerated from the new revision.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Assignment,
    AssignmentStatus,
    CarePlan,
    Invitation,
    InvitationStatus,
    PlanPrerequisite,
    PlanTaskTemplate,
    Task,
    TaskStatus,
)
from app.services import audit
from app.services.timeutils import as_utc, enumerate_service_dates, local_occurrence_interval


class GenerationError(ValueError):
    pass


def _tz(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def generate_tasks(
    session: Session,
    plan: CarePlan,
    *,
    now: datetime,
    horizon_days: int = 14,
    actor_id: str = "system",
) -> list[Task]:
    """Create missing occurrences for ``plan`` over the rolling horizon.

    Only occurrences whose end is still in the future are generated. Existing
    rows for the (revision, template, date) key are left untouched, so calling
    this repeatedly is safe and concurrent calls cannot double-book.
    """
    now = as_utc(now)
    tz = _tz(plan.timezone)
    today_service = now.astimezone(tz).date()
    days = enumerate_service_dates(today_service, horizon_days)

    # Serialize concurrent generations of the same plan; the natural-key
    # unique constraint remains the hard backstop against duplicate tasks.
    locked = session.scalar(select(CarePlan).where(CarePlan.id == plan.id).with_for_update())
    if locked is None:  # pragma: no cover - defensive
        raise GenerationError(f"plan {plan.id} vanished")
    plan = locked

    templates = list(session.scalars(
        select(PlanTaskTemplate).where(PlanTaskTemplate.plan_id == plan.id)
    ))
    if not templates:
        return []

    prereqs = list(session.scalars(
        select(PlanPrerequisite).where(PlanPrerequisite.plan_id == plan.id)
    ))
    prereq_by_code: dict[str, list[str]] = {}
    for p in prereqs:
        prereq_by_code.setdefault(p.task_code, []).append(p.prerequisite_code)

    # What already exists? (one round trip covers the whole horizon)
    existing = session.scalars(
        select(Task).where(
            Task.plan_id == plan.id,
            Task.scheduled_date >= days[0].isoformat(),
            Task.scheduled_date <= days[-1].isoformat(),
        )
    )
    existing_keys = {(t.template_id, t.scheduled_date) for t in existing}

    created: list[Task] = []
    # template code -> (date -> task id) for prerequisite resolution within the batch.
    id_by_code_date: dict[str, dict[str, int]] = {}
    # Include tasks already in the DB when resolving prerequisites.
    for t in session.scalars(select(Task).where(Task.plan_id == plan.id)):
        tmpl = next((x for x in templates if x.id == t.template_id), None)
        if tmpl is not None:
            id_by_code_date.setdefault(tmpl.code, {})[t.scheduled_date] = t.id

    for day in days:
        iso_weekday = day.isoweekday()
        for template in templates:
            if template.weekday_mask and iso_weekday not in template.weekday_mask:
                continue
            start, end = local_occurrence_interval(
                plan.timezone, day,
                template.window_start_minute, template.duration_minutes,
            )
            if end <= now:
                continue  # never generate past occurrences
            date_str = day.isoformat()
            if (template.id, date_str) in existing_keys:
                continue

            task = Task(
                plan_id=plan.id,
                template_id=template.id,
                unit_id=plan.unit_id,
                scheduled_date=date_str,
                scheduled_start=start,
                scheduled_end=end,
                status=TaskStatus.PLANNED.value,
                qualification_codes=list(template.qualification_codes),
                prerequisites=[],
                created_at=now,
            )
            session.add(task)
            session.flush()  # get task.id; unique constraint below is the concurrency backstop
            id_by_code_date.setdefault(template.code, {})[date_str] = task.id
            created.append(task)

    # Resolve prerequisite links within the plan (same service-local date).
    for task in created:
        template = next(t for t in templates if t.id == task.template_id)
        links: list[dict] = []
        for pre_code in prereq_by_code.get(template.code, []):
            pre_id = id_by_code_date.get(pre_code, {}).get(task.scheduled_date)
            if pre_id is not None:
                links.append({"task_id": pre_id, "template_code": pre_code})
        task.prerequisites = links

    if created:
        audit.record(
            session, action="tasks.generated", actor_type="system", actor_id=actor_id,
            reason=f"generated {len(created)} occurrences for plan {plan.external_id} r{plan.revision}",
            detail={"plan_id": plan.id, "count": len(created)}, now=now,
        )
    return created


def revise_plan(
    session: Session,
    *,
    external_id: str,
    client_name: str,
    timezone: str,
    templates: list[dict],
    now: datetime,
    actor_id: str = "coordinator",
) -> CarePlan:
    """Create a new revision of an existing plan and reconcile future tasks.

    Started occurrences (in_progress/completed or already begun by wall clock)
    remain on the old revision; every other old-revision task — including
    assigned future ones — is cancelled, releasing invitations and capacity,
    and fresh tasks are generated from the new revision.
    """
    now = as_utc(now)
    old = session.scalar(
        select(CarePlan).where(CarePlan.external_id == external_id, CarePlan.active.is_(True))
    )
    if old is None:
        raise GenerationError(f"no active plan with external_id={external_id}")

    old_tasks = list(session.scalars(select(Task).where(Task.plan_id == old.id)))
    started = [t for t in old_tasks if _has_started(t, now)]
    for t in old_tasks:
        if t in started:
            continue
        _cancel_future_task(session, t, now=now, reason="plan_revision", actor_id=actor_id)

    old.active = False

    next_revision = _next_revision(session, external_id)
    plan = CarePlan(
        external_id=external_id,
        revision=next_revision,
        active=True,
        client_name=client_name,
        unit_id=old.unit_id,
        timezone=timezone,
        created_at=now,
    )
    session.add(plan)
    session.flush()
    _write_template_payload(session, plan, templates)
    generate_tasks(session, plan, now=now, actor_id=actor_id)

    audit.record(
        session, action="plan.revised", actor_type="coordinator", actor_id=actor_id,
        reason=f"plan {external_id} revised to r{next_revision}; "
               f"{len(old_tasks) - len(started)} future tasks cancelled, {len(started)} started kept",
        detail={
            "external_id": external_id,
            "old_revision": old.revision,
            "new_revision": next_revision,
            "cancelled_task_ids": [t.id for t in old_tasks if t not in started],
            "kept_started_task_ids": [t.id for t in started],
        },
        now=now,
    )
    return plan


def _next_revision(session: Session, external_id: str) -> int:
    rows = session.scalars(
        select(CarePlan.revision).where(CarePlan.external_id == external_id)
    ).all()
    return (max(rows) if rows else 0) + 1


def _has_started(task: Task, now: datetime) -> bool:
    if task.status in (TaskStatus.IN_PROGRESS.value, TaskStatus.COMPLETED.value):
        return True
    return as_utc(task.scheduled_start) <= as_utc(now)


def _cancel_future_task(
    session: Session, task: Task, *, now: datetime, reason: str, actor_id: str
) -> None:
    if task.status == TaskStatus.CANCELLED.value:
        return
    invitations = list(session.scalars(
        select(Invitation).where(Invitation.task_id == task.id)
    ))
    for inv in invitations:
        if inv.status == InvitationStatus.PENDING.value:
            inv.status = InvitationStatus.CANCELLED.value
            inv.responded_at = now
    assignments = list(session.scalars(
        select(Assignment).where(Assignment.task_id == task.id)
    ))
    # Removing the assignment releases the worker's capacity; the audit entry
    # below keeps the before-snapshot (who was assigned) for change history.
    for a in assignments:
        if a.status != AssignmentStatus.CANCELLED.value:
            audit.record(
                session, action="assignment.released", actor_type="system", actor_id=actor_id,
                task_id=task.id, reason=reason,
                detail={"worker_id": a.worker_id, "invitation_id": a.invitation_id},
                now=now,
            )
        session.delete(a)
    task.status = TaskStatus.CANCELLED.value
    audit.record(
        session, action="task.cancelled", actor_type="system", actor_id=actor_id,
        task_id=task.id, reason=reason, detail={"plan_id": task.plan_id}, now=now,
    )


def _write_template_payload(session: Session, plan: CarePlan, templates: list[dict]) -> None:
    seen_codes: set[str] = set()
    pre_pairs: list[tuple[str, str]] = []
    for payload in templates:
        code = payload["code"]
        if code in seen_codes:
            raise GenerationError(f"duplicate template code {code}")
        seen_codes.add(code)
        ws, we = int(payload["window_start_minute"]), int(payload["window_end_minute"])
        duration = int(payload["duration_minutes"])
        if not (0 <= ws < we <= 1440):
            raise GenerationError(f"template {code}: window must satisfy 0 <= start < end <= 1440")
        if duration <= 0 or ws + duration > 1440:
            raise GenerationError(f"template {code}: duration must fit inside the service day")
        if ws + duration > we:
            # The latest possible start (window end) must still fit the duration.
            raise GenerationError(f"template {code}: duration {duration} does not fit window {ws}..{we}")
        session.add(PlanTaskTemplate(
            plan_id=plan.id,
            code=code,
            name=payload["name"],
            window_start_minute=ws,
            window_end_minute=we,
            duration_minutes=duration,
            weekday_mask=sorted(set(payload.get("weekday_mask") or [])),
            qualification_codes=list(payload.get("qualification_codes") or []),
        ))
        for pre in payload.get("prerequisite_codes") or []:
            pre_pairs.append((code, pre))

    session.flush()
    for task_code, pre_code in pre_pairs:
        if task_code not in seen_codes or pre_code not in seen_codes:
            raise GenerationError(
                f"prerequisite {pre_code} -> {task_code} references unknown template code"
            )
        session.add(PlanPrerequisite(
            plan_id=plan.id, task_code=task_code, prerequisite_code=pre_code,
        ))


def create_plan(
    session: Session,
    *,
    external_id: str,
    client_name: str,
    unit_id: int,
    timezone: str,
    templates: list[dict],
    now: datetime,
    actor_id: str = "coordinator",
) -> CarePlan:
    existing = session.scalar(select(CarePlan).where(CarePlan.external_id == external_id))
    if existing is not None:
        raise GenerationError(
            f"plan {external_id} already exists (revision {existing.revision}); use revision endpoint"
        )
    # Validate tz early for a clean error.
    _tz(timezone)
    plan = CarePlan(
        external_id=external_id,
        revision=1,
        active=True,
        client_name=client_name,
        unit_id=unit_id,
        timezone=timezone,
        created_at=now,
    )
    session.add(plan)
    session.flush()
    _write_template_payload(session, plan, templates)
    generate_tasks(session, plan, now=now, actor_id=actor_id)
    audit.record(
        session, action="plan.created", actor_type="coordinator", actor_id=actor_id,
        reason=f"plan {external_id} created", detail={"plan_id": plan.id}, now=now,
    )
    return plan
