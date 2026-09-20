"""Core consent logic: event-sourced writes, idempotency, OCC, projections.

All functions take an active SQLAlchemy session and a ``Principal`` resolved by
the auth layer.  Mutating functions commit themselves unless noted (the batch
importer drives one transaction for the whole batch).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, cast, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .errors import Conflict, Forbidden, Gone, NotFound, Unprocessable
from .models import (
    ApiKey,
    AuditLog,
    ConsentEvent,
    ConsentPolicy,
    ConsentState,
    EventIdempotency,
    Organization,
    PolicyVersion,
    Purpose,
    Subject,
    SubjectExportCopy,
    utcnow,
)
from .security import sha256_hex, subject_pseudonym
from .locking import ADVISORY_KEY_PREFIX, ADVISORY_ORG_PREFIX
from . import services_rebuild


@dataclass(frozen=True)
class Principal:
    organization_id: int
    role: str
    key_id: int

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


# --------------------------------------------------------------------------- helpers


def _audit(db: Session, principal: Principal, action: str, detail: dict[str, Any]) -> None:
    """Append an audit entry. ``detail`` must never contain personal fields."""
    db.add(
        AuditLog(
            organization_id=principal.organization_id,
            actor_pseudonym=sha256_hex(f"key:{principal.key_id}"),
            actor_role=principal.role,
            action=action,
            detail_json=json.dumps(detail, separators=(",", ":"), sort_keys=True),
        )
    )


def _org_lock(db: Session, organization_id: int) -> None:
    """Global-per-org advisory lock.

    Every state-changing write takes this, and the rebuild routine holds it for
    its whole transaction, so a rebuild is strictly serialized against writers:
    events either commit before the rebuild replays them, or wait for the lock
    and get applied incrementally afterwards.  None are lost.
    """
    db.execute(
        select(
            func.pg_advisory_xact_lock(
                cast(func.hashtextextended(f"{ADVISORY_ORG_PREFIX}:{organization_id}", 0),
                     BigInteger)
            )
        )
    )


def _key_lock(db: Session, organization_id: int, token: str) -> None:
    db.execute(
        select(
            func.pg_advisory_xact_lock(
                cast(func.hashtextextended(
                    f"{ADVISORY_KEY_PREFIX}:{organization_id}:{token}", 0), BigInteger)
            )
        )
    )


def _fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    return sha256_hex(canonical)


def _get_purpose(db: Session, organization_id: int, key: str, *, for_update: bool = False) -> Purpose:
    stmt = select(Purpose).where(
        Purpose.organization_id == organization_id, Purpose.key == key
    )
    if for_update:
        stmt = stmt.with_for_update()
    purpose = db.execute(stmt).scalar_one_or_none()
    if purpose is None:
        raise NotFound(f"purpose '{key}' not found")
    return purpose


def _get_or_create_subject(db: Session, organization_id: int, subject_ref: str) -> Subject:
    subject = db.execute(
        select(Subject).where(
            Subject.organization_id == organization_id,
            Subject.subject_ref == subject_ref,
        ).with_for_update()
    ).scalar_one_or_none()
    if subject is None:
        # Random salt per incarnation: after erasure a later re-registration
        # gets a different pseudonym, so retained history can never be folded
        # back onto the new identity.
        pseudonym = subject_pseudonym(
            organization_id, f"{subject_ref}#{uuid.uuid4().hex}"
        )
        subject = Subject(
            organization_id=organization_id,
            subject_ref=subject_ref,
            subject_pseudonym=pseudonym,
        )
        db.add(subject)
        db.flush()
    return subject


def _get_subject(db: Session, organization_id: int, subject_ref: str) -> Subject | None:
    return db.execute(
        select(Subject).where(
            Subject.organization_id == organization_id,
            Subject.subject_ref == subject_ref,
        )
    ).scalar_one_or_none()


def _state_view(
    state: ConsentState | None,
    *,
    organization_id: int,
    subject_ref: str,
    purpose_key: str,
    now: datetime,
) -> dict[str, Any]:
    if state is None:
        return {
            "valid": False,
            "status": "no_record",
            "reason": "no_record",
            "organization_id": organization_id,
            "subject_ref": subject_ref,
            "purpose_key": purpose_key,
            "state_version": None,
            "grant": None,
            "withdrawal": None,
        }
    expired = state.status == "granted" and state.expires_at is not None and state.expires_at <= now
    if state.status == "granted" and not expired:
        status, reason, valid = "granted", "active_grant", True
    elif expired:
        status, reason, valid = "granted", "expired", False
    else:
        status, reason, valid = "withdrawn", "withdrawn", False

    return {
        "valid": valid,
        "status": status,
        "reason": reason,
        "organization_id": organization_id,
        "subject_ref": subject_ref,
        "purpose_key": purpose_key,
        "state_version": state.version,
        "grant": {
            "event_id": state.granted_event_id,
            "event_sequence": state.granted_event_sequence,
            "policy_version_id": state.policy_version_id,
            "granted_at": state.granted_at.isoformat() if state.granted_at else None,
            "expires_at": state.expires_at.isoformat() if state.expires_at else None,
        },
        "withdrawal": {
            "event_id": state.withdrawn_event_id,
            "event_sequence": state.withdrawn_event_sequence,
            "withdrawn_at": state.withdrawn_at.isoformat() if state.withdrawn_at else None,
        } if state.withdrawn_at else None,
    }


# ----------------------------------------------------------------- policy / purpose


def create_purpose(db: Session, principal: Principal, key: str, description: str) -> Purpose:
    if not principal.is_admin:
        raise Forbidden("auditor role is read-only")
    purpose = Purpose(organization_id=principal.organization_id, key=key, description=description)
    db.add(purpose)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise Conflict(f"purpose '{key}' already exists")
    _audit(db, principal, "purpose.create", {"purpose_key": key, "purpose_id": purpose.id})
    db.commit()
    db.refresh(purpose)
    return purpose


def list_purposes(db: Session, principal: Principal) -> list[Purpose]:
    return list(
        db.execute(
            select(Purpose)
            .where(Purpose.organization_id == principal.organization_id)
            .order_by(Purpose.key)
        ).scalars()
    )


def publish_policy_version(
    db: Session, principal: Principal, purpose_key: str, body: str
) -> PolicyVersion:
    if not principal.is_admin:
        raise Forbidden("auditor role is read-only")
    _org_lock(db, principal.organization_id)
    purpose = _get_purpose(db, principal.organization_id, purpose_key, for_update=True)
    policy = db.execute(
        select(ConsentPolicy).where(
            ConsentPolicy.organization_id == principal.organization_id,
            ConsentPolicy.purpose_id == purpose.id,
        ).with_for_update()
    ).scalar_one_or_none()
    if policy is None:
        policy = ConsentPolicy(
            organization_id=principal.organization_id, purpose_id=purpose.id
        )
        db.add(policy)
        db.flush()
    last = db.execute(
        select(func.max(PolicyVersion.version)).where(PolicyVersion.policy_id == policy.id)
    ).scalar()
    version = (last or 0) + 1
    pv = PolicyVersion(
        policy_id=policy.id,
        organization_id=principal.organization_id,
        version=version,
        body=body,
    )
    db.add(pv)
    db.flush()
    _audit(
        db,
        principal,
        "policy.publish",
        {"purpose_key": purpose_key, "policy_id": policy.id, "version": version},
    )
    db.commit()
    db.refresh(pv)
    return pv


def list_policy_versions(
    db: Session, principal: Principal, purpose_key: str
) -> list[PolicyVersion]:
    purpose = _get_purpose(db, principal.organization_id, purpose_key)
    return list(
        db.execute(
            select(PolicyVersion)
            .join(ConsentPolicy, ConsentPolicy.id == PolicyVersion.policy_id)
            .where(
                ConsentPolicy.organization_id == principal.organization_id,
                ConsentPolicy.purpose_id == purpose.id,
            )
            .order_by(PolicyVersion.version)
        ).scalars()
    )


# ----------------------------------------------------------------------- event write


@dataclass
class WriteOutcome:
    response: dict[str, Any]
    replayed: bool


def _resolve_policy_version(
    db: Session, organization_id: int, purpose: Purpose, version_no: int
) -> PolicyVersion:
    pv = db.execute(
        select(PolicyVersion)
        .join(ConsentPolicy, ConsentPolicy.id == PolicyVersion.policy_id)
        .where(
            ConsentPolicy.organization_id == organization_id,
            ConsentPolicy.purpose_id == purpose.id,
            PolicyVersion.version == version_no,
        )
    ).scalar_one_or_none()
    if pv is None:
        raise Unprocessable(
            f"policy version {version_no} for purpose '{purpose.key}' is not published",
            code="unknown_policy_version",
        )
    return pv


def _load_state_for_update(
    db: Session, subject_id: int, purpose_id: int
) -> ConsentState | None:
    return db.execute(
        select(ConsentState).where(
            ConsentState.subject_id == subject_id,
            ConsentState.purpose_id == purpose_id,
        ).with_for_update()
    ).scalar_one_or_none()


def write_event(
    db: Session,
    principal: Principal,
    *,
    event_id: str,
    expected_version: int,
    subject_ref: str,
    purpose_key: str,
    action: str,
    policy_version: int | None = None,
    expires_at: datetime | None = None,
    commit: bool = True,
) -> WriteOutcome:
    """Apply a grant or withdrawal with idempotency + optimistic concurrency.

    * Same ``event_id`` + identical payload -> original stored result (no new event).
    * Same ``event_id`` + different payload -> 409.
    * ``expected_version`` != current projection version -> 409 (stale writer).
    * Any failure raises and the caller rolls back; nothing reaches history.
    """
    if not principal.is_admin:
        raise Forbidden("auditor role is read-only")

    payload = {
        "action": action,
        "subject_ref": subject_ref,
        "purpose_key": purpose_key,
        "expected_version": expected_version,
        "policy_version": policy_version,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }
    fingerprint = _fingerprint(payload)
    now = utcnow()

    _org_lock(db, principal.organization_id)
    _key_lock(db, principal.organization_id, f"{subject_ref}:{purpose_key}")

    # Idempotency: fast path for retries.
    existing = db.execute(
        select(EventIdempotency).where(
            EventIdempotency.organization_id == principal.organization_id,
            EventIdempotency.event_id == event_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.erased:
            raise Gone(
                "the subject of this event has been erased; the event will not be replayed",
                code="subject_erased",
            )
        if existing.request_fingerprint != fingerprint:
            raise Conflict(
                "event_id was already used with a different request body",
                code="idempotency_mismatch",
            )
        return WriteOutcome(response=json.loads(existing.response_json), replayed=True)

    purpose = _get_purpose(db, principal.organization_id, purpose_key)
    subject = _get_or_create_subject(db, principal.organization_id, subject_ref)
    state = _load_state_for_update(db, subject.id, purpose.id)
    current_version = state.version if state else 0
    if current_version != expected_version:
        raise Conflict(
            f"expected version {expected_version} but current version is {current_version}",
            code="version_conflict",
        )

    pv: PolicyVersion | None = None
    if action == "grant":
        assert policy_version is not None
        if expires_at is not None and expires_at <= now:
            raise Unprocessable("expires_at must be in the future", code="already_expired")
        pv = _resolve_policy_version(db, principal.organization_id, purpose, policy_version)
    elif action == "withdrawal":
        if state is None:
            raise Conflict("no consent exists to withdraw", code="no_consent")
        if state.status == "withdrawn":
            raise Conflict("consent is already withdrawn", code="already_withdrawn")
    else:  # pragma: no cover - schema-validated
        raise Unprocessable(f"unknown action {action}")

    next_seq = (
        db.execute(
            select(func.coalesce(func.max(ConsentEvent.sequence), 0) + 1).where(
                ConsentEvent.organization_id == principal.organization_id,
                ConsentEvent.subject_pseudonym == subject.subject_pseudonym,
                ConsentEvent.purpose_key == purpose.key,
            )
        ).scalar_one()
    )

    event = ConsentEvent(
        organization_id=principal.organization_id,
        subject_pseudonym=subject.subject_pseudonym,
        purpose_key=purpose.key,
        event_type="grant" if action == "grant" else "withdrawal",
        sequence=next_seq,
        policy_version=policy_version,
        expires_at=expires_at if action == "grant" else None,
        occurred_at=now,
    )
    db.add(event)
    db.flush()

    if state is None:
        state = ConsentState(
            organization_id=principal.organization_id,
            subject_id=subject.id,
            purpose_id=purpose.id,
            status="withdrawn",
            version=0,
        )
        db.add(state)

    if action == "grant":
        state.status = "granted"
        state.policy_version_id = pv.id
        state.granted_event_id = event.id
        state.granted_event_sequence = event.sequence
        state.granted_at = now
        state.expires_at = expires_at
        state.withdrawn_event_id = None
        state.withdrawn_event_sequence = None
        state.withdrawn_at = None
    else:
        state.status = "withdrawn"
        state.withdrawn_event_id = event.id
        state.withdrawn_event_sequence = event.sequence
        state.withdrawn_at = now
    state.version += 1
    state.updated_at = now
    db.flush()

    response = _state_view(
        state,
        organization_id=principal.organization_id,
        subject_ref=subject_ref,
        purpose_key=purpose.key,
        now=now,
    )
    response["basis"] = {
        "event_sequence": event.sequence,
        "event_type": event.event_type,
        "policy_version": policy_version,
        "policy_version_id": pv.id if pv else None,
        "event_id": event_id,
    }

    db.add(
        EventIdempotency(
            organization_id=principal.organization_id,
            subject_id=subject.id,
            event_id=event_id,
            request_fingerprint=fingerprint,
            response_json=json.dumps(response, default=str),
            consent_event_id=event.id,
        )
    )
    _audit(
        db,
        principal,
        f"consent.{action}",
        {
            "purpose_key": purpose.key,
            "subject_pseudonym": subject.subject_pseudonym,
            "sequence": event.sequence,
            "expected_version": expected_version,
            "replayed": False,
        },
    )
    if commit:
        db.commit()
    return WriteOutcome(response=response, replayed=False)


# -------------------------------------------------------------------------- queries


def verify_consent(
    db: Session, principal: Principal, subject_ref: str, purpose_key: str
) -> dict[str, Any]:
    now = utcnow()
    subject = _get_subject(db, principal.organization_id, subject_ref)
    purpose = _get_purpose(db, principal.organization_id, purpose_key)
    state = None
    if subject is not None:
        state = db.execute(
            select(ConsentState).where(
                ConsentState.subject_id == subject.id,
                ConsentState.purpose_id == purpose.id,
            )
        ).scalar_one_or_none()
    view = _state_view(
        state,
        organization_id=principal.organization_id,
        subject_ref=subject_ref,
        purpose_key=purpose_key,
        now=now,
    )
    if state is not None:
        # Surface the policy version number in addition to the id.
        pv_no = None
        if state.policy_version_id is not None:
            pv_no = db.execute(
                select(PolicyVersion.version).where(
                    PolicyVersion.id == state.policy_version_id
                )
            ).scalar_one_or_none()
        view["basis"] = {
            "grant_event_id": state.granted_event_id,
            "grant_event_sequence": state.granted_event_sequence,
            "withdrawal_event_id": state.withdrawn_event_id,
            "withdrawal_event_sequence": state.withdrawn_event_sequence,
            "policy_version": pv_no,
            "policy_version_id": state.policy_version_id,
        }
        view["valid"] = (
            state.status == "granted"
            and (state.expires_at is None or state.expires_at > now)
        )
        if state.status == "withdrawn":
            view["reason"] = "withdrawn"
        elif not view["valid"]:
            view["reason"] = "expired"
        else:
            view["reason"] = "active_grant"
    return view


def history_for(
    db: Session, principal: Principal, subject_ref: str, purpose_key: str
) -> list[ConsentEvent]:
    """Immutable history. Requires the identifiable mapping to still exist;
    after erasure only pseudonymous access remains."""
    subject = _get_subject(db, principal.organization_id, subject_ref)
    if subject is None:
        raise NotFound("subject not found (identifiable mapping no longer exists)")
    _get_purpose(db, principal.organization_id, purpose_key)
    return list(
        db.execute(
            select(ConsentEvent)
            .where(
                ConsentEvent.organization_id == principal.organization_id,
                ConsentEvent.subject_pseudonym == subject.subject_pseudonym,
                ConsentEvent.purpose_key == purpose_key,
            )
            .order_by(ConsentEvent.sequence)
        ).scalars()
    )


def history_by_pseudonym(
    db: Session, principal: Principal, pseudonym: str, purpose_key: str | None = None
) -> list[ConsentEvent]:
    stmt = select(ConsentEvent).where(
        ConsentEvent.organization_id == principal.organization_id,
        ConsentEvent.subject_pseudonym == pseudonym,
    )
    if purpose_key:
        stmt = stmt.where(ConsentEvent.purpose_key == purpose_key)
    return list(db.execute(stmt.order_by(ConsentEvent.sequence)).scalars())


# --------------------------------------------------------------------- batch import


def batch_import(
    db: Session, principal: Principal, items: list[dict[str, Any]]
) -> dict[str, Any]:
    if len(items) > 500:
        raise Unprocessable("batch is limited to 500 events", code="batch_too_large")
    seen_event_ids: set[str] = set()
    results: list[dict[str, Any]] = []
    imported = 0
    try:
        for index, item in enumerate(items):
            if item["event_id"] in seen_event_ids:
                raise Conflict(
                    f"duplicate event_id '{item['event_id']}' within batch at index {index}",
                    code="duplicate_in_batch",
                )
            seen_event_ids.add(item["event_id"])
            outcome = write_event(
                db,
                principal,
                event_id=item["event_id"],
                expected_version=item["expected_version"],
                subject_ref=item["subject_ref"],
                purpose_key=item["purpose_key"],
                action=item["action"],
                policy_version=item.get("policy_version"),
                expires_at=item.get("expires_at"),
                commit=False,
            )
            if not outcome.replayed:
                imported += 1
            results.append(
                {"index": index, "event_id": item["event_id"], "result": outcome.response}
            )
        db.commit()
    except Exception:
        # Any error anywhere -> the whole batch rolls back, nothing in history.
        db.rollback()
        raise
    return {"imported": imported, "results": results}


# ------------------------------------------------------------- subject export copies


def register_export_copy(
    db: Session, principal: Principal, subject_ref: str, label: str
) -> SubjectExportCopy:
    if not principal.is_admin:
        raise Forbidden("auditor role is read-only")
    subject = _get_subject(db, principal.organization_id, subject_ref)
    if subject is None:
        raise NotFound("subject not found")
    copy = SubjectExportCopy(
        organization_id=principal.organization_id,
        subject_id=subject.id,
        copy_label=label[:200],
    )
    db.add(copy)
    db.flush()
    _audit(
        db,
        principal,
        "subject.export_copy",
        {"subject_pseudonym": subject.subject_pseudonym, "copy_id": copy.id},
    )
    db.commit()
    db.refresh(copy)
    return copy


# ------------------------------------------------------------------------ erasure


def delete_subject(
    db: Session, principal: Principal, subject_ref: str
) -> dict[str, int]:
    """Right-to-erasure style removal.

    Destroys the identifiable mapping, the projection, idempotency snapshots
    (which contain the reference) and every export copy.  The immutable event
    history is retained but carries only a one-way pseudonym; audit logs contain
    no personal fields.
    """
    if not principal.is_admin:
        raise Forbidden("auditor role is read-only")
    _org_lock(db, principal.organization_id)
    subject = db.execute(
        select(Subject)
        .where(
            Subject.organization_id == principal.organization_id,
            Subject.subject_ref == subject_ref,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if subject is None:
        raise NotFound("subject not found")

    n_states = db.execute(
        select(func.count())
        .select_from(ConsentState)
        .where(ConsentState.subject_id == subject.id)
    ).scalar_one()
    n_copies = db.execute(
        select(func.count())
        .select_from(SubjectExportCopy)
        .where(SubjectExportCopy.subject_id == subject.id)
    ).scalar_one()
    n_idem = db.execute(
        select(func.count())
        .select_from(EventIdempotency)
        .where(EventIdempotency.subject_id == subject.id)
    ).scalar_one()
    n_history = db.execute(
        select(func.count())
        .select_from(ConsentEvent)
        .where(
            ConsentEvent.organization_id == principal.organization_id,
            ConsentEvent.subject_pseudonym == subject.subject_pseudonym,
        )
    ).scalar_one()

    # States and export copies cascade via FK, but delete explicitly for counts.
    db.execute(delete(ConsentState).where(ConsentState.subject_id == subject.id))
    db.execute(
        delete(SubjectExportCopy).where(SubjectExportCopy.subject_id == subject.id)
    )
    # Idempotency snapshots embed subject_ref -> anonymise them, keeping only
    # the request fingerprint as a tombstone so a delayed retry cannot recreate
    # the erased subject (it gets 410 Gone).
    db.execute(
        update(EventIdempotency)
        .where(EventIdempotency.subject_id == subject.id)
        .values(subject_id=None, response_json="{}", erased=True)
    )
    db.delete(subject)
    db.flush()

    n_audit = db.execute(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.organization_id == principal.organization_id)
    ).scalar_one()

    _audit(
        db,
        principal,
        "subject.delete",
        {
            "subject_pseudonym": subject.subject_pseudonym,
            "deleted_states": int(n_states),
            "deleted_export_copies": int(n_copies),
            "deleted_idempotency_snapshots": int(n_idem),
            "retained_history_events": int(n_history),
        },
    )
    db.commit()
    return {
        "deleted_subjects": 1,
        "deleted_states": int(n_states),
        "retained_history_events": int(n_history),
        "retained_audit_logs": int(n_audit) + 1,
    }


# ----------------------------------------------------------------------- rebuild


def rebuild_states(db: Session, principal: Principal) -> dict[str, Any]:
    if not principal.is_admin:
        raise Forbidden("auditor role is read-only")
    result = services_rebuild.rebuild_organization(db, principal.organization_id)
    _audit(
        db,
        principal,
        "state.rebuild",
        {
            "materialized": result["materialized"],
            "replayed_events": result["replayed_events"],
            "took_ms": result["took_ms"],
        },
    )
    db.commit()
    return result


# ------------------------------------------------------------------------ audit


def list_audit_logs(
    db: Session, principal: Principal, limit: int = 100
) -> list[AuditLog]:
    return list(
        db.execute(
            select(AuditLog)
            .where(AuditLog.organization_id == principal.organization_id)
            .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
            .limit(limit)
        ).scalars()
    )


# ----------------------------------------------------------------- platform bootstrap


def create_organization(db: Session, name: str) -> dict[str, Any]:
    if db.execute(select(Organization).where(Organization.name == name)).scalar_one_or_none():
        raise Conflict(f"organization '{name}' already exists")
    org = Organization(name=name)
    db.add(org)
    db.flush()
    import secrets

    from .security import api_key_hash

    admin_raw = f"cv_admin_{secrets.token_urlsafe(32)}"
    auditor_raw = f"cv_auditor_{secrets.token_urlsafe(32)}"
    db.add(ApiKey(organization_id=org.id, key_hash=api_key_hash(admin_raw), role="admin", label="admin"))
    db.add(ApiKey(organization_id=org.id, key_hash=api_key_hash(auditor_raw), role="auditor", label="auditor"))
    db.commit()
    return {
        "organization_id": org.id,
        "name": name,
        "admin_api_key": admin_raw,
        "auditor_api_key": auditor_raw,
    }
