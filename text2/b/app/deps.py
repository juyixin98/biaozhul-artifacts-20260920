"""Shared API helpers: serializers and the app-wide controllable clock."""
from __future__ import annotations

from fastapi import Request

from app.clock import Clock, SystemClock
from app.models import Assignment, CarePlan, Task
from app.services import time_utils

# Single clock instance for the process. Production uses SystemClock; the test
# suite swaps ``app.state.clock`` for a FakeClock and the sync endpoint allows
# advancing a deployed instance in demos.
clock: Clock = SystemClock()


def serialize_task(task: Task) -> dict:
    return {
        "id": task.id,
        "plan_id": task.plan_id,
        "plan_version_id": task.plan_version_id,
        "occurrence_date": task.occurrence_date,
        "starts_at": time_utils.ensure_utc(task.starts_at),
        "ends_at": time_utils.ensure_utc(task.ends_at),
        "latest_start_at": time_utils.ensure_utc(task.latest_start_at),
        "duration_minutes": task.duration_minutes,
        "status": task.status.value if hasattr(task.status, "value") else task.status,
        "manually_adjusted": bool(task.manually_adjusted),
        "required_qualification_ids": [q.qualification_id for q in task.qualifications],
        "prerequisite_task_ids": [p.required_task_id for p in task.prerequisites],
    }


def serialize_assignment(asm: Assignment) -> dict:
    return {
        "id": asm.id,
        "task_id": asm.task_id,
        "worker_id": asm.worker_id,
        "status": asm.status.value if hasattr(asm.status, "value") else asm.status,
        "invited_at": time_utils.ensure_utc(asm.invited_at),
        "expires_at": time_utils.ensure_utc(asm.expires_at),
        "responded_at": time_utils.ensure_utc(asm.responded_at)
        if asm.responded_at
        else None,
        "invited_by": asm.invited_by,
        "task_starts_at": time_utils.ensure_utc(asm.task_starts_at),
        "task_ends_at": time_utils.ensure_utc(asm.task_ends_at),
    }


def serialize_plan(plan: CarePlan) -> dict:
    active = plan.active_version()
    versions = []
    for v in sorted(plan.versions, key=lambda x: x.version_number):
        versions.append(
            {
                "id": v.id,
                "version_number": v.version_number,
                "status": v.status.value if hasattr(v.status, "value") else v.status,
                "timezone": v.timezone,
                "period_days": v.period_days,
                "anchor_date": time_utils.ensure_utc(v.anchor_date),
                "change_note": v.change_note,
                "created_at": time_utils.ensure_utc(v.created_at),
                "qualification_ids": [q.qualification_id for q in v.qualifications],
                "prerequisite_plan_ids": [p.required_plan_id for p in v.prerequisites],
                "slots": [
                    {
                        "id": s.id,
                        "weekday": s.weekday,
                        "start_at": datetime_time(s.start_at),
                        "latest_start_at": datetime_time(s.latest_start_at),
                        "duration_minutes": s.duration_minutes,
                    }
                    for s in v.slots
                ],
            }
        )
    return {
        "id": plan.id,
        "unit_id": plan.unit_id,
        "title": plan.title,
        "active": bool(plan.active),
        "created_at": time_utils.ensure_utc(plan.created_at),
        "active_version_number": active.version_number if active else None,
        "versions": versions,
    }


def datetime_time(t):
    from datetime import datetime as _dt

    return _dt.combine(_dt(2000, 1, 1).date(), t).replace(
        tzinfo=time_utils.UTC
    )


def get_clock(request: Request) -> Clock:
    return request.app.state.clock
