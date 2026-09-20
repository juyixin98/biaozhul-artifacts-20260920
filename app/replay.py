"""Pure state derivation from the immutable ledger + transactional rebuild.

The materialised ``consent_states`` table is a cache of a fold over
``consent_events`` (ordered by ``id``). Because the fold is deterministic and
contains no wall-clock logic, rebuilding from scratch always produces exactly
what incremental processing produced.
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import (
    ACTION_GRANT,
    ConsentEvent,
    ConsentState,
    SubjectDeletion,
)


class DerivedState:
    __slots__ = (
        "subject_id",
        "purpose",
        "status",
        "version",
        "granted_event_id",
        "latest_event_id",
        "policy_version",
        "expires_at",
    )

    def __init__(self, event: ConsentEvent) -> None:
        self.subject_id = event.subject_id
        self.purpose = event.purpose
        self.version = event.state_version
        self.latest_event_id = event.event_id
        if event.action == ACTION_GRANT:
            self.status = "granted"
            self.granted_event_id = event.event_id
            self.policy_version = event.policy_version
            self.expires_at = event.expires_at
        else:
            self.status = "withdrawn"
            self.granted_event_id = None
            self.policy_version = None
            self.expires_at = None


def derive_states(
    events: Iterable[ConsentEvent], tombstoned_subject_ids: set[int]
) -> dict[tuple[int, str], DerivedState]:
    """Fold ordered events into one derived state per (subject_id, purpose).

    Events belonging to an erased (tombstoned) subject are skipped entirely, so
    an erasure survives a rebuild — a replay cannot resurrect their state.
    """
    result: dict[tuple[int, str], DerivedState] = {}
    for event in events:
        if event.subject_id in tombstoned_subject_ids:
            continue
        key = (event.subject_id, event.purpose)
        state = result.get(key)
        if state is None:
            result[key] = DerivedState(event)
        else:
            # Same fold rule as the incremental write path.
            state.version = event.state_version
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
    return result


def rebuild_organization(db: Session, organization_id: int) -> dict[str, int]:
    """Discard and recompute one organization's materialised states.

    Takes an EXCLUSIVE table lock on the ledger for the duration. The lock
    blocks all INSERTs into ``consent_events`` until the rebuild commits, so an
    event arriving concurrently simply waits and is then present in the table —
    it can neither be missed nor double applied.
    """
    # EXCLUSIVE conflicts with RowExclusiveLock taken by INSERT; writers block
    # until commit, but plain SELECTs remain possible.
    db.execute(text("LOCK TABLE consent_events IN EXCLUSIVE MODE"))

    events = list(
        db.scalars(
            select(ConsentEvent)
            .where(ConsentEvent.organization_id == organization_id)
            .order_by(ConsentEvent.id.asc())
        )
    )
    tombstoned = set(
        db.scalars(
            select(SubjectDeletion.subject_id).where(
                SubjectDeletion.organization_id == organization_id
            )
        )
    )

    derived = derive_states(events, tombstoned)

    # Purge the old materialisation inside the same locked transaction.
    existing = db.query(ConsentState).filter(
        ConsentState.organization_id == organization_id
    )
    existing.delete(synchronize_session=False)
    db.flush()

    if derived:
        db.add_all(
            [
                ConsentState(
                    organization_id=organization_id,
                    subject_id=key[0],
                    purpose=key[1],
                    status=s.status,
                    version=s.version,
                    granted_event_id=s.granted_event_id,
                    latest_event_id=s.latest_event_id,
                    policy_version=s.policy_version,
                    expires_at=s.expires_at,
                )
                for key, s in derived.items()
            ]
        )

    skipped = sum(1 for e in events if e.subject_id in tombstoned)
    return {
        "replayed_events": len(events),
        "materialized_states": len(derived),
        "skipped_tombstoned_subjects": skipped,
    }
