"""Task lifecycle operations: prerequisite gating and completion."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import clock
from app.models import Task, TaskStatus
from app.services.assignments import (
    AuthorizationError,
    TaskNotFoundError,
    _event,
    _lock_task,
    authorize_unit,
)


class TaskStateError(RuntimeError):
    pass


def prerequisites_blocking(db: Session, task: Task) -> list[int]:
    """Return ids of incomplete prerequisite tasks."""
    if not task.prerequisite_task_ids:
        return []
    done = set(
        db.scalars(
            select(Task.id).where(
                Task.id.in_(task.prerequisite_task_ids),
                Task.status == TaskStatus.completed,
            )
        ).all()
    )
    return [tid for tid in task.prerequisite_task_ids if tid not in done]


def complete_task(
    db: Session, task_id: int, *, coordinator_id: int | None = None
) -> Task:
    task = _lock_task(db, task_id)
    authorize_unit(db, coordinator_id, task.unit_id)
    if task.status != TaskStatus.assigned:
        raise TaskStateError(
            f"task must be assigned before completion (current: {task.status.value})"
        )
    blocking = prerequisites_blocking(db, task)
    if blocking:
        raise TaskStateError(
            f"prerequisite tasks not completed: {blocking}"
        )
    task.status = TaskStatus.completed
    task.updated_at = clock.now()
    _event(db, task_id=task.id, action="task_completed",
           actor=f"coordinator:{coordinator_id}" if coordinator_id else "system")
    db.commit()
    db.refresh(task)
    return task
