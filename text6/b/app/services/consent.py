"""Consent write path, verification and history rebuild.

Concurrency model
-----------------
Each consent stream ``(org, subject, purpose)`` is serialised with a
PostgreSQL transaction-scoped advisory lock derived from a stable hash of the
stream key, so concurrent grants/withdraws against the same stream cannot
both observe the same current version.

The rebuilder instead takes a SHARE ROW EXCLUSIVE-conflicting EXCLUSIVE table
lock on ``consent_events`` for the duration of the rebuild transaction, which
blocks writers while it replaces ``consent_states``; queued writers then
proceed against the fresh state, so no event can be lost during a rebuild.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    AuditAction,
    AuditLog,
    ConsentEvent,
    ConsentState,
    EventType,
    PolicyVersion,
    Subject,
)
from app.services.errors import (
    ConflictError,
    GoneError,
    NotFoundError,
    SemanticError,
)
from app.services.state import (
    FoldEvent,
    canonical_fingerprint,
    evaluate_validity,
    fold_stream,
    utcnow,
)


def _stream_lock_key(organization_id: int, subject_id: int, purpose: str) -> int:
    digest = hashlib.sha256(
        f"{organization_id}:{subject_id}:{purpose}".encode("utf-8")
    ).digest()[:8]
    key = int.from_bytes(digest, "big", signed=False)
    # Map into the signed bigint space pg_advisory_xact_lock expects.
    return key - (1 << 63)


def _acquire_stream_lock(db: Session, organization_id: int, subject_id: int, purpose: str) -> None:
    db.execute(
        text("SELECT pg_advisory_xact_lock(:k)"),
        {"k": _stream_lock_key(organization_id, subject_id, purpose)},
    )


def _get_active_subject(db: Session, organization_id: int, subject_id: int) -> Subject:
    subject = (
        db.query(Subject)
        .filter(Subject.id == subject_id, Subject.organization_id == organization_id)
        .one_or_none()
    )
    if subject is None:
        raise NotFoundError("subject not found in your organization")
    if subject.erased:
        raise GoneError("subject has been erased")
    return subject


def _get_policy(db: Session, organization_id: int, version: str) -> PolicyVersion:
    policy = (
        db.query(PolicyVersion)
        .filter(
            PolicyVersion.organization_id == organization_id,
            PolicyVersion.version == version,
        )
        .one_or_none()
    )
    if policy is None:
        raise SemanticError(f"policy version {version!r} is not published for your organization")
    return policy


def _stream_events(
    db: Session, organization_id: int, subject_id: int, purpose: str
) -> list[ConsentEvent]:
    return (
        db.query(ConsentEvent)
        .filter(
            ConsentEvent.organization_id == organization_id,
            ConsentEvent.subject_id == subject_id,
            ConsentEvent.purpose == purpose,
        )
        .order_by(ConsentEvent.version.asc())
        .all()
    )


def _event_to_fold(ev: ConsentEvent) -> FoldEvent:
    policy_version = ev.policy_version.version if ev.policy_version is not None else None
    return FoldEvent(
        event_id=ev.event_id,
        event_type=ev.event_type,
        version=ev.version,
        policy_version=policy_version,
        expires_at=ev.expires_at,
        created_at=ev.created_at,
    )


def _audit(db: Session, organization_id: int, api_key_id: int, action: AuditAction, detail: dict):
    db.add(
        AuditLog(
            organization_id=organization_id,
            api_key_id=api_key_id,
            action=action,
            detail=detail,
        )
    )


def append_event(
    db: Session,
    *,
    organization_id: int,
    api_key_id: int,
    event_id: str,
    subject_id: int,
    purpose: str,
    expected_version: int,
    event_type: EventType,
    policy_version: str | None = None,
    expires_at: datetime | None = None,
) -> tuple[ConsentEvent, bool]:
    """Append a grant/withdraw event with idempotent replay semantics.

    Returns ``(event, replayed)``. Raises ``ConflictError`` on stale
    ``expected_version`` or on a reused event id with different content;
    raises ``SemanticError`` if the operation is invalid against the current
    stream. Failures raise before any row is committed, so no failed event
    enters the ledger.
    """
    fingerprint = _fingerprint(
        event_id=event_id,
        subject_id=subject_id,
        purpose=purpose,
        expected_version=expected_version,
        event_type=event_type,
        policy_version=policy_version,
        expires_at=expires_at,
    )
    # Cheap pre-lock validation gives clean errors on the common path.
    _validate_targets(
        db,
        organization_id=organization_id,
        subject_id=subject_id,
        event_type=event_type,
        policy_version=policy_version,
    )

    # A first cheap idempotency check outside the lock handles the common
    # plain-retry case; correctness is rechecked under the lock below.
    existing = _find_existing(db, organization_id, event_id)
    if existing is not None:
        return _resolve_replay(
            existing, fingerprint=fingerprint, subject_id=subject_id, purpose=purpose
        ), True

    try:
        event, replayed = _append_locked(
            db,
            organization_id=organization_id,
            api_key_id=api_key_id,
            event_id=event_id,
            subject_id=subject_id,
            purpose=purpose,
            expected_version=expected_version,
            event_type=event_type,
            policy_version=policy_version,
            expires_at=expires_at,
            fingerprint=fingerprint,
            audit=True,
        )
        db.commit()
    except IntegrityError:
        # Last-resort race: another transaction raced us on the unique
        # constraints. Discard our write and resolve against the winner.
        db.rollback()
        winner = _find_existing(db, organization_id, event_id)
        if winner is not None:
            event = _resolve_replay(
                winner, fingerprint=fingerprint, subject_id=subject_id, purpose=purpose
            )
            return event, True
        raise ConflictError("concurrent modification; retry with the current version")

    db.refresh(event)
    return event, replayed


def _fingerprint(
    *,
    event_id: str,
    subject_id: int,
    purpose: str,
    expected_version: int,
    event_type: EventType,
    policy_version: str | None,
    expires_at: datetime | None,
) -> str:
    return canonical_fingerprint(
        event_id=event_id,
        subject_id=subject_id,
        purpose=purpose,
        expected_version=expected_version,
        event_type=event_type,
        policy_version=policy_version,
        expires_at=expires_at,
    )


def _find_existing(db: Session, organization_id: int, event_id: str) -> ConsentEvent | None:
    return (
        db.query(ConsentEvent)
        .filter(
            ConsentEvent.organization_id == organization_id,
            ConsentEvent.event_id == event_id,
        )
        .one_or_none()
    )


def _resolve_replay(
    existing: ConsentEvent,
    *,
    fingerprint: str,
    subject_id: int,
    purpose: str,
) -> ConsentEvent:
    """Return the prior event if this is an identical retry, else conflict.

    Same event id + different content, or same event id reused against a
    different stream, are both client errors.
    """
    if existing.fingerprint != fingerprint:
        raise ConflictError(
            f"event_id {existing.event_id!r} already exists with different content"
        )
    if existing.subject_id != subject_id or existing.purpose != purpose:
        raise ConflictError(
            f"event_id {existing.event_id!r} is already used on a different consent stream"
        )
    return existing


def _validate_targets(
    db: Session,
    *,
    organization_id: int,
    subject_id: int,
    event_type: EventType,
    policy_version: str | None,
) -> PolicyVersion | None:
    _get_active_subject(db, organization_id, subject_id)
    if event_type is EventType.grant:
        if not policy_version:
            raise SemanticError("grant events must bind to a published policy version")
        return _get_policy(db, organization_id, policy_version)
    if policy_version:
        # withdraws never bind policies, but validate the reference exists so
        # a malformed request fails before touching the stream.
        _get_policy(db, organization_id, policy_version)
    return None


def _append_locked(
    db: Session,
    *,
    organization_id: int,
    api_key_id: int,
    event_id: str,
    subject_id: int,
    purpose: str,
    expected_version: int,
    event_type: EventType,
    policy_version: str | None,
    expires_at: datetime | None,
    fingerprint: str,
    audit: bool,
) -> tuple[ConsentEvent, bool]:
    """Append within the caller's transaction. Caller owns commit/rollback.

    Shared by the single-event endpoint and the atomic batch importer.
    """
    policy = _validate_targets(
        db,
        organization_id=organization_id,
        subject_id=subject_id,
        event_type=event_type,
        policy_version=policy_version,
    )

    _acquire_stream_lock(db, organization_id, subject_id, purpose)

    # Re-check under the lock -- a concurrent request may have just inserted.
    existing = _find_existing(db, organization_id, event_id)
    if existing is not None:
        return _resolve_replay(
            existing, fingerprint=fingerprint, subject_id=subject_id, purpose=purpose
        ), True

    events = _stream_events(db, organization_id, subject_id, purpose)
    current_version = events[-1].version if events else 0

    if expected_version != current_version:
        raise ConflictError(
            f"expected stream version {expected_version}, current version is {current_version}"
        )

    folded = fold_stream([_event_to_fold(e) for e in events])
    valid, reason = evaluate_validity(folded)

    if event_type is EventType.withdraw:
        # Only an active, currently-valid grant can be withdrawn.
        if folded is None or not valid:
            why = "no active grant to withdraw" if folded is None else f"cannot withdraw: {reason}"
            raise SemanticError(why)
    else:
        # A grant over an already-valid grant is a semantic misuse: publish a
        # new event id after withdraw/expiry instead. This keeps "a late retry
        # of an old grant cannot silently restore consent" unambiguous.
        if valid:
            raise SemanticError(
                "an active grant already exists for this stream; withdraw first or wait for expiry"
            )

    new_version = current_version + 1
    event = ConsentEvent(
        event_id=event_id,
        organization_id=organization_id,
        subject_id=subject_id,
        purpose=purpose,
        event_type=event_type,
        version=new_version,
        policy_version_id=policy.id if policy else None,
        expires_at=expires_at,
        fingerprint=fingerprint,
    )
    db.add(event)
    db.flush()  # assigns event.id for the state FK

    state = (
        db.query(ConsentState)
        .filter(
            ConsentState.organization_id == organization_id,
            ConsentState.subject_id == subject_id,
            ConsentState.purpose == purpose,
        )
        .one_or_none()
    )
    if state is None:
        state = ConsentState(
            organization_id=organization_id,
            subject_id=subject_id,
            purpose=purpose,
        )
        db.add(state)
    state.last_event_id = event.id
    state.version = new_version

    if audit:
        _audit(
            db,
            organization_id,
            api_key_id,
            AuditAction.consent_granted if event_type is EventType.grant
            else AuditAction.consent_withdrawn,
            {"purpose": purpose, "version": new_version, "replayed": False},
        )

    return event, False


def verify_consent(
    db: Session, *, organization_id: int, subject_id: int, purpose: str, now: datetime | None = None
):
    """Return the current validity verdict derived from the event ledger.

    Validity is evaluated on read, so expiry takes effect immediately without
    any cleanup task.
    """
    subject = (
        db.query(Subject)
        .filter(Subject.id == subject_id, Subject.organization_id == organization_id)
        .one_or_none()
    )
    if subject is None:
        raise NotFoundError("subject not found in your organization")

    events = (
        db.query(ConsentEvent)
        .filter(
            ConsentEvent.organization_id == organization_id,
            ConsentEvent.subject_id == subject_id,
            ConsentEvent.purpose == purpose,
        )
        .order_by(ConsentEvent.version.asc())
        .all()
    )
    folded = fold_stream([_event_to_fold(e) for e in events])
    valid, reason = evaluate_validity(folded, now=now)

    return {
        "subject_id": subject_id,
        "purpose": purpose,
        "valid": valid,
        "reason": reason,
        "current_version": folded.version if folded else 0,
        "grant_event_id": folded.grant_event_id if folded else None,
        "withdraw_event_id": folded.withdraw_event_id if folded else None,
        "policy_version": folded.policy_version if folded else None,
        "expires_at": folded.expires_at if folded else None,
        "evaluated_at": now or utcnow(),
        "erased": subject.erased,
    }


def history(db: Session, *, organization_id: int, subject_id: int) -> list[ConsentEvent]:
    subject = (
        db.query(Subject)
        .filter(Subject.id == subject_id, Subject.organization_id == organization_id)
        .one_or_none()
    )
    if subject is None:
        raise NotFoundError("subject not found in your organization")
    return (
        db.query(ConsentEvent)
        .filter(
            ConsentEvent.organization_id == organization_id,
            ConsentEvent.subject_id == subject_id,
        )
        .order_by(ConsentEvent.id.asc())
        .all()
    )


def rebuild_states(db: Session, *, organization_id: int, api_key_id: int) -> dict:
    """Rebuild ``consent_states`` from the immutable event ledger.

    Runs in a single transaction that holds an EXCLUSIVE table lock on
    ``consent_events``: concurrent writers block until the rebuild commits,
    then append on top of the rebuilt table -- no event is lost. The fold is
    computed with the exact same :func:`fold_stream` used incrementally.
    """
    # Snapshot events for the organization under the writer-excluding lock.
    db.execute(text("LOCK TABLE consent_events IN EXCLUSIVE MODE"))

    events = (
        db.query(ConsentEvent)
        .filter(ConsentEvent.organization_id == organization_id)
        .order_by(ConsentEvent.version.asc())
        .all()
    )

    grouped: dict[tuple[int, str], list[ConsentEvent]] = defaultdict(list)
    for ev in events:
        grouped[(ev.subject_id, ev.purpose)].append(ev)

    # Replace this organization's materialised states wholesale.
    db.query(ConsentState).filter(ConsentState.organization_id == organization_id).delete(
        synchronize_session=False
    )

    rebuilt = 0
    for (subject_id, purpose), stream_events in grouped.items():
        folded = fold_stream([_event_to_fold(e) for e in stream_events])
        if folded is None:
            continue
        last = stream_events[-1]
        db.add(
            ConsentState(
                organization_id=organization_id,
                subject_id=subject_id,
                purpose=purpose,
                last_event_id=last.id,
                version=folded.version,
            )
        )
        rebuilt += 1

    _audit(
        db,
        organization_id,
        api_key_id,
        AuditAction.states_rebuilt,
        {"events": len(events), "streams": rebuilt},
    )
    db.commit()
    return {"events_replayed": len(events), "streams_rebuilt": rebuilt}
