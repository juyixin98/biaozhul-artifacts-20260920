"""Rebuild the mutable consent-state projection from immutable history.

Containment rules:

* The rebuild runs in one transaction holding the per-org advisory lock, so a
  concurrent writer either commits before replay starts (its event is included)
  or blocks until rebuild finishes (its event is then applied through the normal
  write path).  No event can be lost or applied twice.
* History events whose subject pseudonym no longer maps to a live subject are
  retained but not materialised — after erasure there is no identifiable row a
  state could legitimately reference.
* Rebuilt states are byte-for-byte equivalent in *behaviour* to incrementally
  maintained ones: same status, version (= number of events for that key),
  grant/withdrawal basis and timestamps.
"""

from __future__ import annotations

import time
from collections.abc import Iterable

from sqlalchemy import BigInteger, cast, delete, func, select
from sqlalchemy.orm import Session

from .models import (
    ConsentEvent,
    ConsentPolicy,
    ConsentState,
    PolicyVersion,
    Purpose,
    Subject,
)
from .locking import ADVISORY_ORG_PREFIX


def _purpose_map(db: Session, organization_id: int) -> dict[str, tuple[int, int]]:
    """purpose_key -> (purpose_id, policy_id) (a policy exists once published)."""
    rows = db.execute(
        select(Purpose.key, Purpose.id, ConsentPolicy.id)
        .join(ConsentPolicy, ConsentPolicy.purpose_id == Purpose.id)
        .where(Purpose.organization_id == organization_id)
    ).all()
    return {key: (pid, pol_id) for key, pid, pol_id in rows}


def _policy_version_map(
    db: Session, organization_id: int
) -> dict[tuple[int, int], int]:
    """(policy_id, version_no) -> policy_version_id."""
    rows = db.execute(
        select(PolicyVersion.policy_id, PolicyVersion.version, PolicyVersion.id).where(
            PolicyVersion.organization_id == organization_id
        )
    ).all()
    return {(pol_id, v): pv_id for pol_id, v, pv_id in rows}


def _apply_events(
    db: Session,
    organization_id: int,
    events: Iterable[ConsentEvent],
    purposes: dict[str, tuple[int, int]],
    policy_versions: dict[tuple[int, int], int],
    subjects: dict[str, Subject],
) -> tuple[int, int]:
    states: dict[tuple[str, str], ConsentState] = {}
    replayed = 0
    for ev in events:
        replayed += 1
        subject = subjects.get(ev.subject_pseudonym)
        mapping = purposes.get(ev.purpose_key)
        # History retained after erasure or for removed purposes: not materialised.
        if subject is None or mapping is None:
            continue
        purpose_id, _policy_id = mapping
        key = (ev.subject_pseudonym, ev.purpose_key)
        state = states.get(key)
        if state is None:
            state = ConsentState(
                organization_id=organization_id,
                subject_id=subject.id,
                purpose_id=purpose_id,
                status="withdrawn",
                version=0,
            )
            states[key] = state
            db.add(state)

        if ev.event_type == "grant":
            _purpose_id, policy_id = mapping
            pv_id = None
            if ev.policy_version is not None:
                pv_id = policy_versions.get((policy_id, ev.policy_version))
            state.status = "granted"
            state.policy_version_id = pv_id
            state.granted_event_id = ev.id
            state.granted_event_sequence = ev.sequence
            state.granted_at = ev.occurred_at
            state.expires_at = ev.expires_at
            state.withdrawn_event_id = None
            state.withdrawn_event_sequence = None
            state.withdrawn_at = None
        else:
            # Withdrawal flips status but preserves the grant evidence, exactly
            # as the incremental write path does.
            state.status = "withdrawn"
            state.withdrawn_event_id = ev.id
            state.withdrawn_event_sequence = ev.sequence
            state.withdrawn_at = ev.occurred_at
        state.version += 1
        state.updated_at = ev.occurred_at
    return len(states), replayed


def rebuild_organization(db: Session, organization_id: int) -> dict[str, int]:
    started = time.monotonic()
    # Serialize against all writers for the whole rebuild transaction.
    db.execute(
        select(
            func.pg_advisory_xact_lock(
                cast(func.hashtextextended(f"{ADVISORY_ORG_PREFIX}:{organization_id}", 0),
                     BigInteger)
            )
        )
    )

    purposes = _purpose_map(db, organization_id)
    policy_versions = _policy_version_map(db, organization_id)
    subjects = {
        s.subject_pseudonym: s
        for s in db.execute(
            select(Subject).where(Subject.organization_id == organization_id)
        ).scalars()
    }

    # Drop the projection ...
    db.execute(
        delete(ConsentState).where(ConsentState.organization_id == organization_id)
    )
    db.flush()

    # ... and replay the immutable stream in global order.
    events = db.execute(
        select(ConsentEvent)
        .where(ConsentEvent.organization_id == organization_id)
        .order_by(
            ConsentEvent.subject_pseudonym,
            ConsentEvent.purpose_key,
            ConsentEvent.sequence,
        )
    ).scalars()

    materialized, replayed = _apply_events(
        db, organization_id, events, purposes, policy_versions, subjects
    )
    db.flush()
    took_ms = int((time.monotonic() - started) * 1000)
    return {
        "materialized": materialized,
        "replayed_events": replayed,
        "took_ms": took_ms,
    }
