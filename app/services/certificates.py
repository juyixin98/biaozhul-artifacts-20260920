"""Certificate issuance, revocation and verification.

Exactly one certificate row exists per enrollment (unique constraint).
Issuance is idempotent: retries return the existing certificate. A
supervisor correction that breaks completion revokes the certificate; a
correction that restores completion re-validates the same certificate —
no duplicate is ever issued.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import clock
from ..models import (
    CERT_REVOKED,
    CERT_VALID,
    Certificate,
    Enrollment,
    StepResult,
)


def _generate_serial() -> str:
    return f"SP-{uuid.uuid4().hex[:16].upper()}"


def _content_digest(enrollment: Enrollment) -> str:
    version = enrollment.version
    content = {
        "enrollment_id": str(enrollment.id),
        "learner_id": enrollment.learner_id,
        "program_id": str(version.program_id),
        "version_id": str(version.id),
        "version_number": version.version_number,
        "steps": [s.step_key for s in sorted(version.steps, key=lambda s: s.order_index)],
    }
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def passed_step_keys(db: Session, enrollment_id: uuid.UUID) -> set[str]:
    rows = db.scalars(
        select(StepResult).where(
            StepResult.enrollment_id == enrollment_id, StepResult.passed.is_(True)
        )
    ).all()
    return {r.step_key for r in rows}


def completion_met(db: Session, enrollment: Enrollment) -> bool:
    required = {s.step_key for s in enrollment.version.steps}
    return required <= passed_step_keys(db, enrollment.id)


def maybe_issue(db: Session, enrollment: Enrollment) -> Certificate | None:
    """Issue (or re-validate) the certificate if every step is passed."""
    if not completion_met(db, enrollment):
        return None
    existing = db.scalar(
        select(Certificate).where(Certificate.enrollment_id == enrollment.id)
    )
    if existing is not None:
        if existing.status == CERT_REVOKED:
            existing.status = CERT_VALID
            existing.revoked_at = None
        return existing
    cert = Certificate(
        enrollment_id=enrollment.id,
        version_id=enrollment.version_id,
        serial_number=_generate_serial(),
        content_digest=_content_digest(enrollment),
        status=CERT_VALID,
        issued_at=clock.now(),
    )
    db.add(cert)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        # Lost a race with a concurrent issuance: return the winner's row.
        return db.scalar(
            select(Certificate).where(Certificate.enrollment_id == enrollment.id)
        )
    return cert


def revoke_if_valid(db: Session, enrollment_id: uuid.UUID) -> Certificate | None:
    cert = db.scalar(
        select(Certificate).where(Certificate.enrollment_id == enrollment_id)
    )
    if cert is not None and cert.status == CERT_VALID:
        cert.status = CERT_REVOKED
        cert.revoked_at = clock.now()
    return cert
