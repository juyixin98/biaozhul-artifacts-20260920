"""Step submissions, manager corrections, completion and certificates."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..errors import forbidden, not_found, unprocessable
from ..models import (
    Certificate,
    CertificateStatus,
    Enrollment,
    EnrollmentStatus,
    Program,
    ProgramVersion,
    ResultStatus,
    Step,
    StepDependency,
    StepResult,
    User,
    UserRole,
)
from ..schemas import CorrectionCreate, SubmissionCreate


# ------------------------------------------------------------- loading ----


def _load_version_graph(
    db: Session, version_id: int
) -> tuple[ProgramVersion, dict[int, Step], dict[int, set[int]]]:
    version = db.get(ProgramVersion, version_id)
    if version is None:
        raise not_found("version not found")
    steps = list(db.scalars(select(Step).where(Step.version_id == version_id)))
    by_id = {s.id: s for s in steps}
    prereqs: dict[int, set[int]] = {s.id: set() for s in steps}
    for dep in db.scalars(
        select(StepDependency).where(StepDependency.version_id == version_id)
    ):
        prereqs.setdefault(dep.step_id, set()).add(dep.prerequisite_step_id)
    return version, by_id, prereqs


def _lock_enrollment(db: Session, enrollment_id: int) -> Enrollment:
    """Lock the enrollment row, serializing all result/certificate changes."""
    enrollment = db.scalar(
        select(Enrollment).where(Enrollment.id == enrollment_id).with_for_update()
    )
    if enrollment is None:
        raise not_found("enrollment not found")
    return enrollment


def _fingerprint(content: str, claimed: bool) -> str:
    return hashlib.sha256(f"{claimed}|{content}".encode("utf-8")).hexdigest()


# --------------------------------------------------------- submissions ----


def submit_result(
    db: Session, learner: User, enrollment_id: int, step_order: int, payload: SubmissionCreate
) -> StepResult:
    """Record one submission for a step.

    Rules:

    * only the owning learner may submit;
    * the seat must be CONFIRMED;
    * every prerequisite step must currently be PASSED;
    * only steps of the learner's own pinned version are addressable;
    * an exact repeat of the last counted submission is a no-op (the attempt
      counter is not incremented);
    * once a manager has judged a step FAILED, self-claims can no longer pass
      it — a manager correction is required.
    """
    now = utcnow(db)
    enrollment = _lock_enrollment(db, enrollment_id)
    if enrollment.learner_id != learner.id:
        raise forbidden("learners may only submit results for their own enrollment")
    if enrollment.status is not EnrollmentStatus.CONFIRMED:
        raise unprocessable(
            "SEAT_NOT_CONFIRMED",
            f"submissions require a confirmed seat; current status is {enrollment.status.value}",
        )

    _, steps_by_id, prereqs = _load_version_graph(db, enrollment.version_id)
    step = next((s for s in steps_by_id.values() if s.order_index == step_order), None)
    if step is None:
        raise not_found(f"step {step_order} does not exist in this version")

    missing = [
        steps_by_id[pid].order_index
        for pid in prereqs.get(step.id, set())
        if not _is_passed(db, enrollment.id, pid)
    ]
    if missing:
        raise unprocessable(
            "PREREQUISITES_NOT_PASSED",
            f"complete prerequisite steps {sorted(missing)} before step {step_order}",
        )

    result = _get_result(db, enrollment.id, step.id)
    fingerprint = _fingerprint(payload.content, payload.claimed_passed)
    if result is not None and result.submission_fingerprint == fingerprint:
        # Exact duplicate submission: idempotent, nothing is counted again.
        db.commit()
        db.refresh(result)
        return result

    if result is None:
        result = StepResult(
            enrollment_id=enrollment.id,
            step_id=step.id,
            status=ResultStatus.FAILED,
            attempt_count=0,
            latest_submission="",
            updated_at=now,
        )
        db.add(result)
        db.flush()

    result.attempt_count += 1
    result.latest_submission = payload.content
    result.submission_fingerprint = fingerprint
    result.updated_at = now

    if payload.claimed_passed and result.corrected_by is None and result.status is not ResultStatus.PASSED:
        result.status = ResultStatus.PASSED
        result.passed_at = now

    db.flush()
    _reconcile_completion(db, enrollment, steps_by_id, now)
    db.commit()
    db.refresh(result)
    return result


def _get_result(db: Session, enrollment_id: int, step_id: int) -> StepResult | None:
    return db.scalar(
        select(StepResult)
        .where(StepResult.enrollment_id == enrollment_id, StepResult.step_id == step_id)
        .with_for_update()
    )


def _is_passed(db: Session, enrollment_id: int, step_id: int) -> bool:
    return db.scalar(
        select(StepResult.id).where(
            StepResult.enrollment_id == enrollment_id,
            StepResult.step_id == step_id,
            StepResult.status == ResultStatus.PASSED,
        )
    ) is not None


# --------------------------------------------------------- corrections ----


def correct_result(
    db: Session,
    manager: User,
    enrollment_id: int,
    step_order: int,
    payload: CorrectionCreate,
) -> StepResult:
    """Manager overrides a result. A reason is mandatory.

    Completion and certificate validity are reconciled afterwards: downgrading
    a PASSED step revokes the certificate; re-passing restores the very same
    certificate (same serial and content digest).
    """
    now = utcnow(db)
    if manager.role is not UserRole.MANAGER:
        raise forbidden("only managers may correct results")

    enrollment = _lock_enrollment(db, enrollment_id)
    if enrollment.status is not EnrollmentStatus.CONFIRMED:
        raise unprocessable(
            "SEAT_NOT_CONFIRMED",
            f"corrections require a confirmed seat; current status is {enrollment.status.value}",
        )
    _, steps_by_id, _ = _load_version_graph(db, enrollment.version_id)
    step = next((s for s in steps_by_id.values() if s.order_index == step_order), None)
    if step is None:
        raise not_found(f"step {step_order} does not exist in this version")

    result = _get_result(db, enrollment.id, step.id)
    if result is None:
        # Managers may record the first judgement even without a submission.
        result = StepResult(
            enrollment_id=enrollment.id,
            step_id=step.id,
            status=ResultStatus.FAILED,
            attempt_count=0,
            latest_submission="",
            updated_at=now,
        )
        db.add(result)
        db.flush()

    result.status = payload.status
    result.corrected_by = manager.id
    result.correction_reason = payload.reason
    result.corrected_at = now
    result.updated_at = now
    # A correction supersedes learner submissions: forget the idempotency
    # fingerprint so identical work can be handed in again.
    result.submission_fingerprint = None
    if payload.status is ResultStatus.PASSED:
        if result.passed_at is None:
            result.passed_at = now
    else:
        result.passed_at = None

    db.flush()
    _reconcile_completion(db, enrollment, steps_by_id, now)
    db.commit()
    db.refresh(result)
    return result


# -------------------------------------------------------- completion ------


def _passed_step_ids(db: Session, enrollment_id: int) -> set[int]:
    return set(
        db.scalars(
            select(StepResult.step_id).where(
                StepResult.enrollment_id == enrollment_id,
                StepResult.status == ResultStatus.PASSED,
            )
        )
    )


def _reconcile_completion(
    db: Session,
    enrollment: Enrollment,
    steps_by_id: dict[int, Step],
    now: datetime,
) -> None:
    """Maintain the single certificate row for an enrollment.

    Runs inside the caller's transaction while the enrollment is locked.
    """
    certificate = db.scalar(
        select(Certificate)
        .where(Certificate.enrollment_id == enrollment.id)
        .with_for_update()
    )
    complete = set(steps_by_id).issubset(_passed_step_ids(db, enrollment.id))

    if complete:
        if certificate is None:
            db.add(_build_certificate(db, enrollment, now))
        elif certificate.status is CertificateStatus.REVOKED:
            # Same certificate, same serial and digest, valid again.
            certificate.status = CertificateStatus.VALID
            certificate.revoked_at = None
            certificate.revoke_reason = None
    elif certificate is not None and certificate.status is CertificateStatus.VALID:
        certificate.status = CertificateStatus.REVOKED
        certificate.revoked_at = now
        certificate.revoke_reason = (
            "invalidated automatically: a required step no longer passes"
        )


def _build_certificate(db: Session, enrollment: Enrollment, now: datetime) -> Certificate:
    version = db.get(ProgramVersion, enrollment.version_id)
    program = db.get(Program, version.program_id)
    learner = db.get(User, enrollment.learner_id)
    serial = _generate_serial(enrollment, version)
    return Certificate(
        enrollment_id=enrollment.id,
        version_id=version.id,
        serial=serial,
        learner_name=learner.name,
        program_title=program.title,
        version_number=version.version,
        content_hash=_certificate_content(program, version, learner, serial, now),
        status=CertificateStatus.VALID,
        issued_at=now,
    )


def _generate_serial(enrollment: Enrollment, version: ProgramVersion) -> str:
    """Unique serial derived from immutable (enrollment, pinned version).

    Determinism means a retry after a crash recomputes the identical serial and
    hits the unique constraint instead of producing a second certificate.
    """
    raw = f"SP|v{version.id}|e{enrollment.id}".encode()
    return "SP-" + hashlib.sha256(raw).hexdigest()[:24].upper()


def _certificate_content(
    program: Program,
    version: ProgramVersion,
    learner: User,
    serial: str,
    issued_at: datetime,
) -> str:
    payload = {
        "serial": serial,
        "program_id": program.id,
        "program_title": program.title,
        "version_id": version.id,
        "version_number": version.version,
        "version_content_hash": version.content_hash,
        "learner_id": learner.id,
        "learner_name": learner.name,
        "issued_at": issued_at.isoformat(),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ------------------------------------------------------------- reads ------


def get_progress(db: Session, learner: User, enrollment_id: int) -> dict:
    enrollment = db.get(Enrollment, enrollment_id)
    if enrollment is None:
        raise not_found("enrollment not found")
    if enrollment.learner_id != learner.id and learner.role is not UserRole.MANAGER:
        raise forbidden("only the owner or a manager may view this progress")

    version, steps_by_id, _ = _load_version_graph(db, enrollment.version_id)
    results = {
        r.step_id: r
        for r in db.scalars(
            select(StepResult).where(StepResult.enrollment_id == enrollment.id)
        )
    }
    result_out = []
    passed = 0
    for step in sorted(steps_by_id.values(), key=lambda s: s.order_index):
        r = results.get(step.id)
        if r is not None and r.status is ResultStatus.PASSED:
            passed += 1
        result_out.append(
            {
                "step_id": step.id,
                "step_order": step.order_index,
                "status": r.status if r else ResultStatus.FAILED,
                "attempt_count": r.attempt_count if r else 0,
                "passed_at": r.passed_at if r else None,
                "correction_reason": r.correction_reason if r else None,
                "corrected_at": r.corrected_at if r else None,
            }
        )

    cert = db.scalar(
        select(Certificate)
        .where(Certificate.enrollment_id == enrollment.id)
        .order_by(Certificate.id)
    )
    return {
        "enrollment_id": enrollment.id,
        "version_id": enrollment.version_id,
        "status": enrollment.status,
        "results": result_out,
        "total_steps": len(steps_by_id),
        "passed_steps": passed,
        "certificate_id": cert.id if cert else None,
        "certificate_status": cert.status.value if cert else None,
        "certificate_serial": cert.serial if cert else None,
    }


def get_certificate(db: Session, learner: User, enrollment_id: int) -> Certificate:
    enrollment = db.get(Enrollment, enrollment_id)
    if enrollment is None:
        raise not_found("enrollment not found")
    if enrollment.learner_id != learner.id and learner.role is not UserRole.MANAGER:
        raise forbidden("only the owner or a manager may view this certificate")
    certificate = db.scalar(
        select(Certificate).where(Certificate.enrollment_id == enrollment.id)
    )
    if certificate is None:
        raise not_found("no certificate exists for this enrollment yet")
    return certificate
