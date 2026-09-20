"""Request authentication and unit-level authorization.

The API is a backend for an authenticated coordinator console. A real
deployment would validate a session token; here the acting coordinator is
identified by the ``X-Coordinator-Id`` header (their ``external_id``). The
single rule that matters for the domain is:

    **a coordinator may only schedule units linked to them.**

Workers identify themselves with ``X-Worker-Id`` for accept/decline endpoints.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Coordinator, Unit, Worker
from app.services.errors import AuthorizationError, NotFoundError, ValidationError


@dataclass
class Actor:
    coordinator: Coordinator

    @property
    def id(self) -> int:
        return self.coordinator.id

    def unit_ids(self) -> set[int]:
        return {u.id for u in self.coordinator.units}

    def ensure_unit(self, unit_id: int) -> None:
        if unit_id not in self.unit_ids():
            raise AuthorizationError(
                f"Coordinator is not authorised for unit {unit_id}",
                details={"unit_id": unit_id},
            )

    def ensure_can_access_plan(self, db: Session, plan_id: int) -> None:
        from app.models import CarePlan

        plan = db.get(CarePlan, plan_id)
        if plan is None:
            raise NotFoundError(f"Care plan {plan_id} not found")
        self.ensure_unit(plan.unit_id)

    def ensure_can_access_task(self, db: Session, task_id: int) -> None:
        from app.models import Task

        task = db.get(Task, task_id)
        if task is None:
            raise NotFoundError(f"Task {task_id} not found")
        self.ensure_unit(task.plan.unit_id)


def get_current_coordinator(
    x_coordinator_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Actor:
    if not x_coordinator_id:
        raise AuthorizationError("Missing X-Coordinator-Id header")
    coordinator = db.scalar(
        select(Coordinator).where(Coordinator.external_id == x_coordinator_id)
    )
    if coordinator is None:
        raise NotFoundError(f"Coordinator {x_coordinator_id!r} not found")
    if not coordinator.active:
        raise AuthorizationError("Coordinator account is inactive")
    return Actor(coordinator=coordinator)


def get_worker(
    x_worker_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Worker:
    if not x_worker_id:
        raise AuthorizationError("Missing X-Worker-Id header")
    worker = db.scalar(select(Worker).where(Worker.external_id == x_worker_id))
    if worker is None:
        raise NotFoundError(f"Worker {x_worker_id!r} not found")
    if not worker.active:
        raise AuthorizationError("Worker account is inactive")
    return worker
