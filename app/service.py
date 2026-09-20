"""Core consent domain logic.

Write transaction (single event)
--------------------------------
1. Resolve/get the (live) subject, locking its row.
2. Look up ``event_id`` (idempotency). Same id + identical request hash => the
   original result is returned (``replayed=True``). Same id + different hash
   => 409.
3. Lock the materialised state row (creating it if absent), serialising all
   writers for one (org, subject, purpose).
4. Verify ``expected_version`` against the current state version.
   Stale => 409, nothing inserted.
5. For grants verify the policy version exists; policies are immutable and a
   grant always pins a concrete version.
6. Append the immutable event, update the derived state, commit.

Because step 6 only commits when the whole transaction succeeds, a failed
event never enters the ledger.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.errors import Conflict, NotFound, Unprocessable
from app.models import (
    ACTION_GRANT,
    ConsentEvent,
    ConsentState,
    PolicyVersion,
    Subject,
    utcnow,
)
from app.schemas import GrantIn, WriteResultOut


def request_hash(data: GrantIn) -> str:
    """Stable SHA-256 over the semantically meaningful request fields.

    ``expires_at`` is normalised to UTC first so the same absolute instant
    expressed with different offsets (``...+00:00`` vs ``...+08:00``) produces
    the same hash — those are the same request, not a conflict.
    """
    expires = data.expires_at
    if expires is not None:
        expires = expires.astimezone(timezone.utc).isoformat()
    canonical = json.dumps(
        {
            "subject_key": data.subject_key,
            "purpose": data.purpose,
            "action": data.action,
            "expected_version": data.expected_version,
            "expires_at": expires,
            "policy_version": data.policy_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_live_subject(db: Session, organization_id: int, subject_key: str, lock: bool = True) -> Subject:
    stmt = select(Subject).where(
        Subject.organization_id == organization_id,
        Subject.subject_key == subject_key,
        Subject.erased.is_(False),
    )
    if lock:
        stmt = stmt.with_for_update()
    subject = db.scalars(stmt).one_or_none()
    if subject is None:
        raise NotFound("subject not found")
    return subject


def get_or_create_subject(db: Session, organization_id: int, subject_key: str) -> Subject:
    """Locked lookup-or-create, safe under concurrent inserters.

    Relies on the partial unique index on (organization_id, subject_key) for
    non-erased subjects to settle races. The INSERT runs in a SAVEPOINT so a
    lost race rolls back only that insert — not an in-flight batch.
    """
    subject = db.scalars(
        select(Subject).where(
            Subject.organization_id == organization_id,
            Subject.subject_key == subject_key,
            Subject.erased.is_(False),
        ).with_for_update()
    ).one_or_none()
    if subject is not None:
        return subject

    savepoint = db.begin_nested()
    subject = Subject(organization_id=organization_id, subject_key=subject_key, erased=False)
    db.add(subject)
    try:
        db.flush()
        savepoint.commit()
        return subject
    except IntegrityError:
        savepoint.rollback()
        return get_live_subject(db, organization_id, subject_key, lock=True)


def resolve_policy(db: Session, organization_id: int, requested: int | None) -> int:
    """Return the concrete policy version a grant binds.

    Explicit version => must exist. Omitted => the latest published version;
    a grant with no policy at all is rejected (422).
    """
    if requested is not None:
        exists = db.scalars(
            select(PolicyVersion.version).where(
                PolicyVersion.organization_id == organization_id,
                PolicyVersion.version == requested,
            )
        ).one_or_none()
        if exists is None:
            raise Unprocessable(f"policy version {requested} does not exist")
        return requested

    latest = db.scalars(
        select(PolicyVersion.version)
        .where(PolicyVersion.organization_id == organization_id)
        .order_by(PolicyVersion.version.desc())
        .limit(1)
    ).one_or_none()
    if latest is None:
        raise Unprocessable("organization has no published policy; cannot grant consent")
    return latest


def _load_event_for_update(db: Session, organization_id: int, event_id: str) -> ConsentEvent | None:
    return db.scalars(
        select(ConsentEvent)
        .where(
            ConsentEvent.organization_id == organization_id,
            ConsentEvent.event_id == event_id,
        )
        .with_for_update()
    ).one_or_none()


def _lock_state(
    db: Session, organization_id: int, subject_id: int, purpose: str
) -> ConsentState | None:
    return db.scalars(
        select(ConsentState)
        .where(
            ConsentState.organization_id == organization_id,
            ConsentState.subject_id == subject_id,
            ConsentState.purpose == purpose,
        )
        .with_for_update()
    ).one_or_none()


def _result_from_event(event: ConsentEvent, replayed: bool) -> WriteResultOut:
    return WriteResultOut(
        event_id=event.event_id,
        replayed=replayed,
        state_version=event.state_version,
        status="granted" if event.action == ACTION_GRANT else "withdrawn",
        policy_version=event.policy_version,
        expires_at=event.expires_at,
        created_at=event.created_at,
    )


def apply_event(db: Session, organization_id: int, data: GrantIn) -> WriteResultOut:
    """Idempotent, optimistic, concurrency-safe single write. Commits."""
    try:
        subject = get_or_create_subject(db, organization_id, data.subject_key)

        existing = _load_event_for_update(db, organization_id, data.event_id)
        if existing is not None:
            if existing.request_hash != request_hash(data):
                raise Conflict(
                    "event_id already used with a different request payload"
                )
            db.commit()
            return _result_from_event(existing, replayed=True)

        state = _lock_state(db, organization_id, subject.id, data.purpose)
        current_version = state.version if state is not None else 0
        if data.expected_version != current_version:
            raise Conflict(
                f"expected_version {data.expected_version} is stale; current version is {current_version}"
            )

        policy_version = None
        if data.action == ACTION_GRANT:
            policy_version = resolve_policy(db, organization_id, data.policy_version)

        new_version = current_version + 1
        event = ConsentEvent(
            event_id=data.event_id,
            organization_id=organization_id,
            subject_id=subject.id,
            subject_key_snapshot=subject.subject_key,
            purpose=data.purpose,
            action=data.action,
            policy_version=policy_version,
            expires_at=data.expires_at if data.action == ACTION_GRANT else None,
            expected_version=data.expected_version,
            state_version=new_version,
            request_hash=request_hash(data),
        )
        db.add(event)
        db.flush()  # surface the unique(event_id) race before we touch state

        if state is None:
            state = ConsentState(
                organization_id=organization_id,
                subject_id=subject.id,
                purpose=data.purpose,
                version=new_version,
                latest_event_id=event.event_id,
            )
            db.add(state)

        _fold_into_state(state, event, new_version)
        db.commit()
        return _result_from_event(event, replayed=False)
    except Conflict:
        db.rollback()
        raise
    except Unprocessable:
        db.rollback()
        raise
    except NotFound:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        # A concurrent transaction won the event_id race. Re-read to distinguish
        # an exact replay (return original) from a content conflict.
        existing = db.scalars(
            select(ConsentEvent).where(
                ConsentEvent.organization_id == organization_id,
                ConsentEvent.event_id == data.event_id,
            )
        ).one_or_none()
        if existing is not None:
            if existing.request_hash != request_hash(data):
                raise Conflict("event_id already used with a different request payload")
            return _result_from_event(existing, replayed=True)
        raise Conflict(f"concurrent write conflict: {exc.orig.__class__.__name__}")
    except OperationalError as exc:
        db.rollback()
        # A deadlock/lock-abort chosen by Postgres between concurrent writers.
        # Nothing committed; surface as a retryable conflict rather than a 500.
        raise Conflict(f"concurrent write aborted, please retry: {exc.orig.__class__.__name__}")
    except Exception:
        db.rollback()
        raise


def _fold_into_state(state: ConsentState, event: ConsentEvent, new_version: int) -> None:
    """Same update rule used by ``app.replay.derive_states``."""
    state.version = new_version
    state.latest_event_id = event.event_id
    if event.action == ACTION_GRANT:
        state.status = "granted"
        state.granted_event_id = event.event_id
        state.policy_version = event.policy_version
        state.expires_at = event.expires_at
    else:
        state.status = "withdrawn"
        state.granted_event_id = None
        state.policy_version = None
        state.expires_at = None


def apply_batch(db: Session, organization_id: int, events: list[GrantIn]) -> list[WriteResultOut]:
    """Apply up to 500 events in ONE transaction; any error rolls back the lot.

    Rows are locked in a deterministic order (subject key, purpose, event_id)
    to avoid deadlocks between overlapping batches.
    """
    if len(events) > 500:
        raise Unprocessable("a batch may contain at most 500 events")
    seen_event_ids: set[str] = set()
    results: list[WriteResultOut] = []
    try:
        ordered = sorted(
            events, key=lambda e: (e.subject_key, e.purpose, e.event_id)
        )
        state_cache: dict[tuple[int, str], ConsentState | None] = {}
        subject_cache: dict[str, Subject] = {}

        for data in ordered:
            if data.event_id in seen_event_ids:
                raise Conflict(f"duplicate event_id within batch: {data.event_id}")
            seen_event_ids.add(data.event_id)

            subject = subject_cache.get(data.subject_key)
            if subject is None:
                subject = get_or_create_subject(db, organization_id, data.subject_key)
                subject_cache[data.subject_key] = subject

            existing = _load_event_for_update(db, organization_id, data.event_id)
            if existing is not None:
                if existing.request_hash != request_hash(data):
                    raise Conflict(
                        f"event_id {data.event_id} already used with a different payload"
                    )
                results.append(_result_from_event(existing, replayed=True))
                continue

            cache_key = (subject.id, data.purpose)
            if cache_key not in state_cache:
                state_cache[cache_key] = _lock_state(
                    db, organization_id, subject.id, data.purpose
                )
            state = state_cache[cache_key]
            current_version = state.version if state is not None else 0
            if data.expected_version != current_version:
                raise Conflict(
                    f"event_id {data.event_id}: expected_version {data.expected_version} "
                    f"is stale; current version is {current_version}"
                )

            policy_version = None
            if data.action == ACTION_GRANT:
                policy_version = resolve_policy(db, organization_id, data.policy_version)

            new_version = current_version + 1
            event = ConsentEvent(
                event_id=data.event_id,
                organization_id=organization_id,
                subject_id=subject.id,
                subject_key_snapshot=subject.subject_key,
                purpose=data.purpose,
                action=data.action,
                policy_version=policy_version,
                expires_at=data.expires_at if data.action == ACTION_GRANT else None,
                expected_version=data.expected_version,
                state_version=new_version,
                request_hash=request_hash(data),
            )
            db.add(event)
            db.flush()

            if state is None:
                state = ConsentState(
                    organization_id=organization_id,
                    subject_id=subject.id,
                    purpose=data.purpose,
                    version=new_version,
                    latest_event_id=event.event_id,
                )
                db.add(state)
                state_cache[cache_key] = state

            _fold_into_state(state, event, new_version)
            results.append(_result_from_event(event, replayed=False))

        db.commit()
    except Exception:
        db.rollback()
        raise

    # Return results in the caller's original submission order.
    by_id = {r.event_id: r for r in results}
    return [by_id[e.event_id] for e in events]


def publish_policy(db: Session, organization_id: int, body: str) -> PolicyVersion:
    """Publish a new immutable policy version; numbers are gapless per org."""
    current = db.scalars(
        select(PolicyVersion.version)
        .where(PolicyVersion.organization_id == organization_id)
        .order_by(PolicyVersion.version.desc())
        .limit(1)
        .with_for_update()
    ).one_or_none()
    version = (current or 0) + 1
    pv = PolicyVersion(organization_id=organization_id, version=version, body=body)
    db.add(pv)
    db.commit()
    db.refresh(pv)
    return pv


def list_policies(db: Session, organization_id: int) -> list[PolicyVersion]:
    return list(
        db.scalars(
            select(PolicyVersion)
            .where(PolicyVersion.organization_id == organization_id)
            .order_by(PolicyVersion.version.asc())
        )
    )


def evaluate_consent(
    db: Session, organization_id: int, subject_key: str, purpose: str, *, at: datetime | None = None
) -> dict:
    """Current effective status with the event and policy version it rests on.

    Expiry is evaluated against the clock at query time (or ``at`` in tests),
    so an expired grant is invalid immediately — no cleanup job required.
    """
    now = at or utcnow()
    subject = db.scalars(
        select(Subject).where(
            Subject.organization_id == organization_id,
            Subject.subject_key == subject_key,
            Subject.erased.is_(False),
        )
    ).one_or_none()
    if subject is None:
        raise NotFound("subject not found")

    state = db.scalars(
        select(ConsentState).where(
            ConsentState.organization_id == organization_id,
            ConsentState.subject_id == subject.id,
            ConsentState.purpose == purpose,
        )
    ).one_or_none()

    if state is None:
        return {
            "subject_key": subject_key,
            "purpose": purpose,
            "valid": False,
            "status": "no_record",
            "reason": "no consent event on record",
            "state_version": None,
            "basis_event_id": None,
            "policy_version": None,
            "expires_at": None,
            "evaluated_at": now,
        }

    if state.status == "withdrawn":
        valid, reason = False, "consent was withdrawn"
    elif state.expires_at is not None and state.expires_at <= now:
        valid, reason = False, "grant has expired"
    else:
        valid, reason = True, "grant is current"

    return {
        "subject_key": subject_key,
        "purpose": purpose,
        "valid": valid,
        "status": state.status,
        "reason": reason,
        "state_version": state.version,
        "basis_event_id": state.granted_event_id if valid else state.latest_event_id,
        "policy_version": state.policy_version,
        "expires_at": state.expires_at,
        "evaluated_at": now,
    }


def list_history(
    db: Session, organization_id: int, subject_key: str, purpose: str | None = None
) -> list[ConsentEvent]:
    subject = get_live_subject(db, organization_id, subject_key, lock=False)
    stmt = select(ConsentEvent).where(
        ConsentEvent.organization_id == organization_id,
        ConsentEvent.subject_id == subject.id,
    )
    if purpose is not None:
        stmt = stmt.where(ConsentEvent.purpose == purpose)
    return list(db.scalars(stmt.order_by(ConsentEvent.id.asc())))
