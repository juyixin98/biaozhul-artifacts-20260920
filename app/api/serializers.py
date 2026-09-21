"""ORM -> API schema mapping."""

from __future__ import annotations

from app.models import (
    Assignment,
    AssignmentEvent,
    CarePlan,
    CareWorker,
    Coordinator,
    Task,
)
from app.schemas import (
    AssignmentOut,
    CoordinatorOut,
    EventOut,
    PlanOut,
    TaskOut,
    WorkerOut,
)
from app.services.assignments import AllocationOutcome
from app.services.constraints import Violation
from app.schemas import AllocateResult, ConstraintViolation


def plan_out(plan: CarePlan, prereq_ids: list[int]) -> PlanOut:
    return PlanOut(
        id=plan.id,
        version=plan.version,
        status=plan.status,
        care_recipient_id=plan.care_recipient_id,
        unit_id=plan.unit_id,
        service_timezone=plan.service_timezone,
        recurrence=plan.recurrence,
        day_of_week=plan.day_of_week,
        window_start=plan.window_start,
        window_end=plan.window_end,
        duration_minutes=plan.duration_minutes,
        required_qualifications=list(plan.required_qualifications or []),
        prerequisite_plan_ids=list(prereq_ids),
        created_at=plan.created_at,
        updated_at=plan.updated_at,
    )


def task_out(task: Task) -> TaskOut:
    return TaskOut(
        id=task.id,
        plan_id=task.plan_id,
        plan_version=task.plan_version,
        care_recipient_id=task.care_recipient_id,
        unit_id=task.unit_id,
        occurrence_key=task.occurrence_key,
        starts_at=task.starts_at,
        ends_at=task.ends_at,
        status=task.status,
        prerequisite_task_ids=list(task.prerequisite_task_ids or []),
    )


def assignment_out(assignment: Assignment | None) -> AssignmentOut | None:
    if assignment is None:
        return None
    return AssignmentOut(
        id=assignment.id,
        task_id=assignment.task_id,
        worker_id=assignment.worker_id,
        status=assignment.status,
        invited_at=assignment.invited_at,
        expires_at=assignment.expires_at,
        responded_at=assignment.responded_at,
        created_via=assignment.created_via,
        created_by_coordinator_id=assignment.created_by_coordinator_id,
    )


def violation_out(v: Violation) -> ConstraintViolation:
    return ConstraintViolation(code=v.code, message=v.message, detail=v.detail)


def allocation_out(outcome: AllocationOutcome) -> AllocateResult:
    return AllocateResult(
        allocated=outcome.allocated,
        task=task_out(outcome.task),
        assignment=assignment_out(outcome.assignment),
        violations=[violation_out(v) for v in outcome.violations],
    )


def worker_out(worker: CareWorker) -> WorkerOut:
    return WorkerOut.model_validate(worker)


def coordinator_out(coordinator: Coordinator) -> CoordinatorOut:
    return CoordinatorOut(
        id=coordinator.id,
        name=coordinator.name,
        unit_ids=sorted(u.id for u in coordinator.units),
    )


def event_out(event: AssignmentEvent) -> EventOut:
    return EventOut.model_validate(event)
