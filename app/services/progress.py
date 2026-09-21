"""Learner progress: step submissions, supervisor review/correction,
completion recomputation and certificate validity synchronisation."""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import clock as clock_svc
from app.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.models import (
    Certificate,
    Enrollment,
    ResultCorrection,
    Step,
    StepResult,
    CERT_INVALID,
    CERT_VALID,
    ENROLLMENT_CONFIRMED,
    RESULT_FAILED,
    RESULT_PASSED,
)
from app.services.certificates import issue_certificate, invalidate_certificate, revalidate_certificate


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _get_learner_enrollment(
    db: Session, *, enrollment_id: int, learner_id: int | None = None, lock: bool = True
) -> Enrollment:
    stmt = select(Enrollment).where(Enrollment.id == enrollment_id)
    if lock:
        stmt = stmt.with_for_update()
    enrollment = db.scalar(stmt)
    if enrollment is None:
        raise NotFoundError(f"Enrollment {enrollment_id} not found.")
    if learner_id is not None and enrollment.learner_id != learner_id:
        raise ForbiddenError("You can only submit results for your own enrolment.")
    # Eagerly load results fresh (populate_existing) so callers in the same
    # transaction see rows created after an earlier load.
    db.refresh(enrollment, attribute_names=["results", "certificate"])
    return enrollment


def _load_steps(db: Session, version_id: int) -> list[Step]:
    return list(
        db.scalars(
            select(Step)
            .where(Step.version_id == version_id)
            .options(selectinload(Step.prerequisites))
            .order_by(Step.position)
        ).all()
    )


def _passed_map(results: list[StepResult]) -> dict[int, StepResult]:
    return {
        r.step_id: r
        for r in results
        if r.status == RESULT_PASSED
    }


def _assert_prerequisites_passed(
    step: Step, steps_by_id: dict[int, Step], passed: dict[int, StepResult]
) -> None:
    # prerequisite rows store step keys within the pinned version.
    for prereq in step.prerequisites:
        prereq_step = next(
            (s for s in steps_by_id.values() if s.key == prereq.prerequisite_key),
            None,
        )
        if prereq_step is None:
            raise ValidationError(
                f"Prerequisite step {prereq.prerequisite_key!r} no longer exists."
            )
        if prereq_step.id not in passed:
            raise ConflictError(
                f"Prerequisite step {prereq_step.key!r} must be passed before "
                f"submitting {step.key!r}."
            )


def submit_result(
    db: Session,
    *,
    enrollment_id: int,
    learner_id: int,
    step_key: str,
    content: str,
) -> StepResult:
    """Learner submits work for a step.

    Rules:
    * only the owning learner may submit;
    * enrolment must be confirmed;
    * all declared prerequisites must already be passed;
    * repeat submissions update content and increment the attempt counter
      but never create extra rows and never count twice.
    """
    current = clock_svc.now(db)
    enrollment = _get_learner_enrollment(
        db, enrollment_id=enrollment_id, learner_id=learner_id
    )
    if enrollment.status != ENROLLMENT_CONFIRMED:
        raise ConflictError(
            f"Only confirmed enrolments can submit steps (current status: "
            f"{enrollment.status})."
        )

    steps = _load_steps(db, enrollment.version_id)
    step = next((s for s in steps if s.key == step_key), None)
    if step is None:
        raise NotFoundError(f"Step {step_key!r} does not exist in this version.")

    passed = _passed_map(enrollment.results)
    _assert_prerequisites_passed(step, {s.id: s for s in steps}, passed)

    result = next(
        (r for r in enrollment.results if r.step_id == step.id), None
    )
    if result is None:
        result = StepResult(
            enrollment_id=enrollment.id,
            step_id=step.id,
            status=RESULT_FAILED,
            submission_content=content,
            attempts=1,
            corrected=False,
        )
        db.add(result)
    else:
        result.submission_content = content
        result.attempts += 1
        result.corrected = False  # supervisor's later review takes over again
    result.last_submitted_at = current.replace(tzinfo=None)
    db.flush()
    return result


def review_result(
    db: Session, *, enrollment_id: int, step_key: str, passed: bool
) -> StepResult:
    """Supervisor reviews the latest submission of a step."""
    current = clock_svc.now(db)
    enrollment = _get_learner_enrollment(db, enrollment_id=enrollment_id)
    step = _step_or_404(db, enrollment.version_id, step_key)
    result = _result_or_404(enrollment, step.id)

    result.status = RESULT_PASSED if passed else RESULT_FAILED
    result.corrected = False
    result.reviewed_at = current.replace(tzinfo=None)
    db.flush()

    _recompute_completion(db, enrollment, current)
    return result


def correct_result(
    db: Session,
    *,
    supervisor_id: int,
    enrollment_id: int,
    step_key: str,
    new_status: str,
    reason: str,
) -> StepResult:
    """Supervisor overrides a reviewed result. A reason is mandatory.

    The correction is audited, prerequisite chain consistency is restored
    (a step that loses its pass invalidates dependent downstream passes),
    completion and certificate validity are synchronised in the same
    transaction.
    """
    if not reason or not reason.strip():
        raise ValidationError("A reason is required when correcting a result.")
    if new_status not in (RESULT_PASSED, RESULT_FAILED):
        raise ValidationError("new_status must be 'passed' or 'failed'.")

    current = clock_svc.now(db)
    enrollment = _get_learner_enrollment(db, enrollment_id=enrollment_id)
    steps = _load_steps(db, enrollment.version_id)
    step = next((s for s in steps if s.key == step_key), None)
    if step is None:
        raise NotFoundError(f"Step {step_key!r} does not exist in this version.")
    result = _result_or_404(enrollment, step.id)

    previous_status = result.status
    if previous_status == new_status:
        raise ConflictError(f"Result is already {new_status}.")

    result.status = new_status
    result.corrected = True
    result.reviewed_by = supervisor_id
    result.reviewed_at = current.replace(tzinfo=None)
    db.add(
        ResultCorrection(
            result_id=result.id,
            supervisor_id=supervisor_id,
            previous_status=previous_status,
            new_status=new_status,
            reason=reason.strip(),
        )
    )
    db.flush()

    if new_status == RESULT_FAILED:
        _cascade_failure(db, enrollment, steps, step)

    _recompute_completion(db, enrollment, current)
    return result


def _cascade_failure(
    db: Session,
    enrollment: Enrollment,
    steps: list[Step],
    failed_step: Step,
) -> None:
    """Downstream steps that (transitively) required the failed step can no
    longer be considered passed: the prerequisite chain no longer holds."""
    by_key = {s.key: s for s in steps}
    # Build adjacency key -> dependent keys.
    dependents: dict[str, set[str]] = {s.key: set() for s in steps}
    for s in steps:
        for p in s.prerequisites:
            dependents[p.prerequisite_key].add(s.key)

    to_invalidate: set[str] = set()
    stack = list(dependents[failed_step.key])
    while stack:
        key = stack.pop()
        if key in to_invalidate:
            continue
        to_invalidate.add(key)
        stack.extend(dependents[key])

    results_by_step = {r.step_id: r for r in enrollment.results}
    for key in to_invalidate:
        r = results_by_step.get(by_key[key].id)
        if r is not None and r.status == RESULT_PASSED:
            r.status = RESULT_FAILED
            r.corrected = True
    db.flush()


def _step_or_404(db: Session, version_id: int, step_key: str) -> Step:
    step = db.scalar(
        select(Step).where(
            Step.version_id == version_id, Step.key == step_key
        )
    )
    if step is None:
        raise NotFoundError(f"Step {step_key!r} does not exist in this version.")
    return step


def _result_or_404(enrollment: Enrollment, step_id: int) -> StepResult:
    result = next((r for r in enrollment.results if r.step_id == step_id), None)
    if result is None:
        raise ConflictError("The learner has not submitted this step yet.")
    return result


def _recompute_completion(
    db: Session, enrollment: Enrollment, current: datetime
) -> Certificate | None:
    """All steps passed  <=>  enrolment completed  <=>  certificate valid.

    Certificate issuance is idempotent: one certificate row per enrolment.
    Re-completion after a correction flips the same certificate back to
    valid (serial and digest are preserved).
    """
    steps = _load_steps(db, enrollment.version_id)
    passed_ids = {r.step_id for r in enrollment.results if r.status == RESULT_PASSED}
    all_passed = len(steps) > 0 and all(s.id in passed_ids for s in steps)

    cert = enrollment.certificate

    if all_passed:
        if enrollment.completed_at is None:
            enrollment.completed_at = current.replace(tzinfo=None)
        if cert is None:
            cert = issue_certificate(db, enrollment=enrollment, steps=steps)
        elif cert.status == CERT_INVALID:
            revalidate_certificate(cert, current)
    else:
        if enrollment.completed_at is not None:
            enrollment.completed_at = None
        if cert is not None and cert.status == CERT_VALID:
            invalidate_certificate(db, cert=cert)
    db.flush()
    return cert
