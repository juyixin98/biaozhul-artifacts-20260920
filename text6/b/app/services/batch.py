"""Atomic batch consent import (max 500 items per request).

Every item is validated and applied in a single transaction: the first
conflict or semantic error rolls the whole batch back. A repeated import
(re-request with the same batch / event ids) replays stored events and marks
them ``replayed`` instead of creating duplicates.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AuditAction, AuditLog, ConsentEvent, EventType
from app.services.consent import (
    _append_locked,
    _find_existing,
    _fingerprint,
    _resolve_replay,
)
from app.services.errors import ConflictError, SemanticError
from app.services.state import utcnow

MAX_BATCH = 500


def import_batch(db: Session, *, organization_id: int, api_key_id: int, items) -> dict:
    if len(items) > MAX_BATCH:
        raise SemanticError(f"batch size {len(items)} exceeds maximum of {MAX_BATCH}")

    seen_event_ids: set[str] = set()
    # Cache observed stream versions inside the batch (events don't flush in
    # order we can rely on across distinct streams, so track versions by key).
    stream_versions: dict[tuple[int, str], int] = {}
    results: list[dict] = []
    imported = 0
    replayed = 0

    # Pre-fetch already-stored event ids so a re-imported batch replays them.
    event_ids = [item.event_id for item in items]
    stored = {
        ev.event_id: ev
        for ev in db.query(ConsentEvent)
        .filter(
            ConsentEvent.organization_id == organization_id,
            ConsentEvent.event_id.in_(event_ids),
        )
        .all()
    }

    for item in items:
        if item.event_id in seen_event_ids:
            raise ConflictError(f"duplicate event_id {item.event_id!r} within batch")
        seen_event_ids.add(item.event_id)

        etype = EventType(item.event_type)
        fingerprint = _fingerprint(
            event_id=item.event_id,
            subject_id=item.subject_id,
            purpose=item.purpose,
            expected_version=item.expected_version,
            event_type=etype,
            policy_version=item.policy_version,
            expires_at=item.expires_at,
        )

        existing = stored.get(item.event_id) or _find_existing(
            db, organization_id, item.event_id
        )
        if existing is not None:
            existing = _resolve_replay(
                existing,
                fingerprint=fingerprint,
                subject_id=item.subject_id,
                purpose=item.purpose,
            )
            key = (item.subject_id, item.purpose)
            stream_versions[key] = max(stream_versions.get(key, 0), existing.version)
            replayed += 1
            results.append(
                {
                    "event_id": existing.event_id,
                    "version": existing.version,
                    "event_type": existing.event_type.value,
                    "replayed": True,
                }
            )
            continue

        # For a fresh item inside the batch the expected_version must match
        # the version we have observed so far in-batch (or the ledger's
        # version when the stream was untouched by this batch).
        event, _ = _append_locked(
            db,
            organization_id=organization_id,
            api_key_id=api_key_id,
            event_id=item.event_id,
            subject_id=item.subject_id,
            purpose=item.purpose,
            expected_version=item.expected_version,
            event_type=etype,
            policy_version=item.policy_version,
            expires_at=item.expires_at,
            fingerprint=fingerprint,
            audit=False,
        )
        imported += 1
        stream_versions[(item.subject_id, item.purpose)] = event.version
        results.append(
            {
                "event_id": event.event_id,
                "version": event.version,
                "event_type": event.event_type.value,
                "replayed": False,
            }
        )

    db.add(
        AuditLog(
            organization_id=organization_id,
            api_key_id=api_key_id,
            action=AuditAction.consent_batch_imported,
            detail={
                "items": len(items),
                "imported": imported,
                "replayed": replayed,
                "at": utcnow().isoformat(),
            },
        )
    )
    db.commit()
    return {"imported": imported, "replayed": replayed, "items": results}
