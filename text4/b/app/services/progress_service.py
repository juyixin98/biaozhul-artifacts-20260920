"""Step submissions, supervisor corrections and certificate issuance.

Result rules
------------
* Only the owning learner may submit, and only with a CONFIRMED enrolment.
* A step is open only when every prerequisite step has a PASSED result.
* One ``StepResult`` row per (enrolment, step): repeat submissions return the
  existing row unchanged and are never counted twice.
* Supervisors correct results through ``correct_result``; a non-empty reason
  is mandatory and every correction is audited.  Completion state and the
  enrolment's certificate are reconciled in the same transaction.

Certificate rules
-----------------
* All steps passed  -> exactly one certificate for the pinned version.
* Issuance is retry-safe: it is an upsert keyed by enrolment id; the serial
  number and content digest are deterministic, so retries cannot mint a second
  certificate.
* A correction that breaks completion revokes the existing certificate; a
  correction back restores the same certificate (same serial), so at most one
  *valid* certificate for the version ever exists.
"""
from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import now_utc
from app.errors import ConflictError, ForbiddenError, NotFoundError
from app.models import (
    Certificate,
    Enrollment,
    ResultCorrection,
    Step,
    StepPrerequisite,
    StepResult,
)
from app.services.versions_service import get_program_or_404, require_program_supervisor
from app.statuses import ENR_CONFIRMED, RESULT_PASSED, RESULT_SUBMITTED


# ---------- queries ----------

def _get_enrollment_or_404(session: Session, enrollment_id: int) -> Enrollment:
    enrollment = session.get(Enrollment, enrollment_id)
    if enrollment is None:
        raise NotFoundError(f"enrollment {enrollment_id} not found")
    return enrollment


def _get_step(session: Session, step_id: int, *, version_id: int) -> Step:
    step = session.get(Step, step_id)
    if step is None or step.version_id != version_id:
        raise NotFoundError(f"step {step_id} not found in the enrolled version")
    return step


def _passed_step_ids(session: Session, enrollment_id: int) -> set[int]:
    rows = session.scalars(
        select(StepResult.step_id).where(
            StepResult.enrollment_id == enrollment_id,
            StepResult.status == RESULT_PASSED,
        )
    ).all()
    return set(rows)


def _prerequisite_ids(session: Session, step: Step) -> set[int]:
    rows = session.scalars(
        select(StepPrerequisite.prerequisite_id).where(StepPrerequisite.step_id == step.id)
    ).all()
    return set(rows)


def list_results(session: Session, enrollment_id: int) -> list[StepResult]:
    return list(
        session.scalars(
            select(StepResult)
            .where(StepResult.enrollment_id == enrollment_id)
            .order_by(StepResult.step_id)
        ).all()
    )


# ---------- learner submission ----------

def submit(
    session: Session,
    *,
    enrollment_id: int,
    step_id: int,
    learner_id: int,
    content: str,
) -> StepResult:
    enrollment = _get_enrollment_or_404(session, enrollment_id)
    if enrollment.learner_id != learner_id:
        raise ForbiddenError("learners may only submit their own step results")
    if enrollment.status != ENR_CONFIRMED:
        raise ConflictError("step submission requires a confirmed enrollment")

    step = _get_step(session, step_id, version_id=enrollment.version_id)
    existing = session.scalar(
        select(StepResult).where(
            StepResult.enrollment_id == enrollment_id,
            StepResult.step_id == step_id,
        )
    )
    if existing is not None:
        # Idempotent: repeat submission never re-counts or overwrites content.
        return existing

    passed = _passed_step_ids(session, enrollment_id)
    missing = _prerequisite_ids(session, step) - passed
    if missing:
        raise ConflictError(
            "prerequisite steps not passed yet: "
            + ", ".join(str(sid) for sid in sorted(missing))
        )

    result = StepResult(
        enrollment_id=enrollment_id,
        step_id=step_id,
        status=RESULT_SUBMITTED,
        content=content,
    )
    session.add(result)
    session.commit()
    session.refresh(result)
    return result


# ---------- supervisor correction ----------

def correct_result(
    session: Session,
    *,
    enrollment_id: int,
    step_id: int,
    supervisor_id: int,
    new_status: str,
    reason: str,
) -> StepResult:
    if not reason or not reason.strip():
        raise ConflictError("a reason is required when correcting a result")
    enrollment = _get_enrollment_or_404(session, enrollment_id)
    program = get_program_or_404(session, enrollment.program_id)
    require_program_supervisor(session, program, supervisor_id)
    _get_step(session, step_id, version_id=enrollment.version_id)

    result = session.scalar(
        select(StepResult).where(
            StepResult.enrollment_id == enrollment_id,
            StepResult.step_id == step_id,
        )
    )
    if result is None:
        raise NotFoundError("learner has not submitted this step yet")

    if result.status == new_status:
        # Nothing to correct; still record nothing and keep idempotency.
        return result

    session.add(
        ResultCorrection(
            result_id=result.id,
            supervisor_id=supervisor_id,
            from_status=result.status,
            to_status=new_status,
            reason=reason,
        )
    )
    result.status = new_status
    result.evaluated_at = now_utc()
    result.last_correction_reason = reason
    result.last_corrected_by = supervisor_id
    session.flush()

    reconcile_certificate(session, enrollment)
    session.commit()
    session.refresh(result)
    return result


def evaluate_submission(
    session: Session,
    *,
    enrollment_id: int,
    step_id: int,
    supervisor_id: int,
    new_status: str,
    reason: str = "",
) -> StepResult:
    """Supervisor marks an unevaluated submission.

    A reason is mandatory whenever a result has already been evaluated once
    (i.e. this is a correction); the first evaluation may be a bare mark.
    """
    enrollment = _get_enrollment_or_404(session, enrollment_id)
    program = get_program_or_404(session, enrollment.program_id)
    require_program_supervisor(session, program, supervisor_id)
    _get_step(session, step_id, version_id=enrollment.version_id)

    result = session.scalar(
        select(StepResult).where(
            StepResult.enrollment_id == enrollment_id,
            StepResult.step_id == step_id,
        )
    )
    if result is None:
        raise NotFoundError("learner has not submitted this step yet")
    if result.status == new_status:
        return result

    previously_evaluated = result.status in (RESULT_PASSED, "failed")
    if previously_evaluated:
        if not reason or not reason.strip():
            raise ConflictError("a reason is required when correcting a result")
        session.add(
            ResultCorrection(
                result_id=result.id,
                supervisor_id=supervisor_id,
                from_status=result.status,
                to_status=new_status,
                reason=reason,
            )
        )
    else:
        result.evaluated_at = now_utc()

    result.status = new_status
    result.last_correction_reason = reason or None
    result.last_corrected_by = supervisor_id
    session.flush()

    reconcile_certificate(session, enrollment)
    session.commit()
    session.refresh(result)
    return result


# ---------- certificates ----------

def build_certificate_material(session: Session, enrollment: Enrollment) -> tuple[str, str, bool]:
    """Return (serial_number, content_digest, complete) for an enrollment."""
    steps = list(
        session.scalars(
            select(Step).where(Step.version_id == enrollment.version_id).order_by(Step.position)
        )
    )
    results = {
        r.step_id: r for r in list_results(session, enrollment.id)
    }
    complete = bool(steps) and all(
        results.get(s.id) is not None and results[s.id].status == RESULT_PASSED
        for s in steps
    )

    serial = f"SP-{enrollment.version_id}-{enrollment.id:08d}"
    if complete:
        lines = [f"serial={serial}", f"version={enrollment.version_id}", f"learner={enrollment.learner_id}"]
        for step in steps:
            result = results[step.id]
            lines.append(
                f"{step.position}:{step.id}:{result.status}:{result.submitted_at.isoformat()}"
            )
        digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    else:
        # Stable non-certificate digest for the incomplete state.
        digest = hashlib.sha256(
            f"incomplete:{enrollment.id}:{enrollment.version_id}".encode()
        ).hexdigest()
    return serial, digest, complete


def reconcile_certificate(session: Session, enrollment: Enrollment) -> Certificate | None:
    """Idempotently issue / restore / revoke the enrollment's certificate.

    Must run inside the caller's transaction; does not commit.
    """
    serial, digest, complete = build_certificate_material(session, enrollment)
    certificate = session.scalar(
        select(Certificate).where(Certificate.enrollment_id == enrollment.id)
    )
    now = now_utc()
    if complete:
        if certificate is None:
            certificate = Certificate(
                enrollment_id=enrollment.id,
                version_id=enrollment.version_id,
                serial_number=serial,
                content_digest=digest,
                revoked=False,
                issued_at=now,
            )
            session.add(certificate)
        else:
            # Restore or refresh the single certificate — never a new serial.
            certificate.revoked = False
            certificate.revoked_at = None
            certificate.content_digest = digest
            certificate.version_id = enrollment.version_id
        session.flush()
        return certificate
    if certificate is not None and not certificate.revoked:
        certificate.revoked = True
        certificate.revoked_at = now
        session.flush()
    return certificate


def get_certificate(session: Session, enrollment_id: int) -> Certificate | None:
    return session.scalar(
        select(Certificate).where(Certificate.enrollment_id == enrollment_id)
    )
