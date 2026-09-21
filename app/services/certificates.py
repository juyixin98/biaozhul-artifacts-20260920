"""Certificate issuance: unique serial, pinned-version content digest.

* Exactly one certificate row per enrolment (DB unique constraint).
* ``issue_certificate`` is called inside the completion transaction and
  relies on the unique constraint + retry-safe serial allocation to avoid
  duplicate issuance under retries.
* If completion is later undone by a supervisor correction the certificate
  flips to ``invalid``; restored completion flips the SAME certificate back
  to ``valid`` — no second certificate is ever created.
"""
import hashlib
import json
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import clock as clock_svc
from app.errors import NotFoundError
from app.models import (
    Certificate,
    Enrollment,
    ProgramVersion,
    Step,
    CERT_INVALID,
    CERT_VALID,
)


def version_content_digest(db: Session, version_id: int) -> str:
    version = db.get(ProgramVersion, version_id)
    return version.content_digest if version else ""


def _serial_candidate(db: Session, now: datetime) -> str:
    day = now.strftime("%Y%m%d")
    # Count certificates issued "today" by serial prefix, under the caller's
    # transaction. The unique constraint is the final guarantee.
    prefix = f"SKP-{day}-"
    count = db.scalar(
        select(func.count(Certificate.id)).where(
            Certificate.serial_number.like(prefix + "%")
        )
    )
    return f"{prefix}{(count or 0) + 1:06d}"


def issue_certificate(
    db: Session, *, enrollment: Enrollment, steps: list[Step]
) -> Certificate:
    version = db.get(ProgramVersion, enrollment.version_id)
    digest = version.content_digest or _digest_from_steps(steps)
    current = clock_svc.now(db)

    # Defensive: a row may exist in invalid state — revalidate instead.
    existing = db.scalar(
        select(Certificate).where(
            Certificate.enrollment_id == enrollment.id
        ).with_for_update()
    )
    if existing is not None:
        if existing.status == CERT_INVALID:
            existing.status = CERT_VALID
            existing.issued_at = current.replace(tzinfo=None)
            db.flush()
        return existing

    # Allocate a unique serial; handle same-day collision by re-counting.
    serial = _serial_candidate(db, current)

    cert = Certificate(
        enrollment_id=enrollment.id,
        serial_number=serial,
        version_id=enrollment.version_id,
        content_digest=digest,
        status=CERT_VALID,
        issued_at=current.replace(tzinfo=None),
    )
    # A concurrent transaction may win the one-row-per-enrolment race (or the
    # serial-allocation race). Use a savepoint so a collision aborts only the
    # nested INSERT, letting us re-read the winner instead of failing the
    # whole business transaction.
    for attempt in range(5):
        try:
            with db.begin_nested():
                db.add(cert)
                db.flush()
            break
        except IntegrityError:
            db.expunge(cert)
            winner = db.scalar(
                select(Certificate)
                .where(Certificate.enrollment_id == enrollment.id)
                .with_for_update()
            )
            if winner is not None:
                if winner.status == CERT_INVALID:
                    winner.status = CERT_VALID
                    winner.issued_at = current.replace(tzinfo=None)
                    db.flush()
                return winner
            # No winner visible -> serial-number collision only; retry.
            serial = _serial_candidate(db, current)
            cert = Certificate(
                enrollment_id=enrollment.id,
                serial_number=serial,
                version_id=enrollment.version_id,
                content_digest=digest,
                status=CERT_VALID,
                issued_at=current.replace(tzinfo=None),
            )
    return cert


def invalidate_certificate(db: Session, *, cert: Certificate) -> None:
    cert.status = CERT_INVALID
    cert.issued_at = None
    db.flush()


def revalidate_certificate(cert: Certificate, current: datetime) -> None:
    cert.status = CERT_VALID
    cert.issued_at = current.replace(tzinfo=None)


def _digest_from_steps(steps: list[Step]) -> str:
    canonical = [
        {
            "position": s.position,
            "key": s.key,
            "instruction": s.instruction,
            "pass_condition": s.pass_condition,
            "prerequisite_keys": sorted(p.prerequisite_key for p in s.prerequisites),
        }
        for s in sorted(steps, key=lambda s: s.position)
    ]
    payload = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_certificate(
    db: Session, *, certificate_id: int | None = None, serial_number: str | None = None
) -> Certificate:
    stmt = select(Certificate)
    if certificate_id is not None:
        stmt = stmt.where(Certificate.id == certificate_id)
    elif serial_number is not None:
        stmt = stmt.where(Certificate.serial_number == serial_number)
    else:
        raise ValueError("certificate_id or serial_number required")
    cert = db.scalar(stmt)
    if cert is None:
        raise NotFoundError("Certificate not found.")
    return cert


def certificate_for_enrollment(db: Session, enrollment_id: int) -> Certificate | None:
    return db.scalar(
        select(Certificate).where(Certificate.enrollment_id == enrollment_id)
    )
