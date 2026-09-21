"""Step results and supervisor corrections."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import DomainError
from ..models import (
    ENROLLMENT_CONFIRMED,
    Correction,
    Enrollment,
    StepResult,
)
from . import certificates


def _lock_enrollment(db: Session, enrollment_id: uuid.UUID) -> Enrollment:
    enrollment = db.scalar(
        select(Enrollment).where(Enrollment.id == enrollment_id).with_for_update()
    )
    if enrollment is None:
        raise DomainError(404, "enrollment not found")
    return enrollment


def submit_result(db: Session, enrollment_id: uuid.UUID, learner_id: str,
                  step_key: str, passed: bool) -> tuple[StepResult, bool]:
    """Record a learner's result for one step.

    Returns (result, created). Re-submitting a step that already has a
    result is idempotent: the original record stands and is returned, so
    retries never double-count.
    """
    enrollment = _lock_enrollment(db, enrollment_id)
    if enrollment.learner_id != learner_id:
        raise DomainError(403, "learners can only submit results for themselves")
    if enrollment.status != ENROLLMENT_CONFIRMED:
        raise DomainError(
            409, "seat must be confirmed before submitting step results"
        )
    steps = {s.step_key: s for s in enrollment.version.steps}
    step = steps.get(step_key)
    if step is None:
        raise DomainError(404, f"step '{step_key}' not found in this version")

    results = {
        r.step_key: r
        for r in db.scalars(
            select(StepResult).where(StepResult.enrollment_id == enrollment.id)
        )
    }
    if step_key in results:
        return results[step_key], False  # idempotent, no double counting

    for pre in step.prerequisites:
        pre_result = results.get(pre)
        if pre_result is None or not pre_result.passed:
            raise DomainError(409, f"prerequisite step '{pre}' has not been passed")

    result = StepResult(
        enrollment_id=enrollment.id, step_key=step_key, passed=passed, source="learner"
    )
    db.add(result)
    db.flush()
    if passed:
        certificates.maybe_issue(db, enrollment)
    db.commit()
    db.refresh(result)
    return result, True


def correct_result(db: Session, result_id: uuid.UUID, supervisor_id: str,
                   passed: bool, reason: str) -> StepResult:
    """Supervisor correction of a recorded result.

    A non-empty reason is mandatory. Completion state and certificate
    validity are updated in the same transaction.
    """
    if not reason.strip():
        raise DomainError(422, "a correction reason is required")
    result = db.scalar(
        select(StepResult).where(StepResult.id == result_id).with_for_update()
    )
    if result is None:
        raise DomainError(404, "result not found")
    enrollment = _lock_enrollment(db, result.enrollment_id)

    correction = Correction(
        result_id=result.id,
        passed=passed,
        reason=reason,
        created_by=supervisor_id,
    )
    db.add(correction)
    result.passed = passed
    result.source = "correction"
    db.flush()

    if passed:
        certificates.maybe_issue(db, enrollment)
    else:
        certificates.revoke_if_valid(db, enrollment.id)
    db.commit()
    db.refresh(result)
    return result
