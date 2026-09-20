"""Pure state-fold logic shared by the incremental writer and the rebuilder.

Keeping this free of database I/O is what lets the tests (and the rebuild
endpoint) assert that replay-from-history and incremental processing produce
exactly the same answer.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from app.models import EventType


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def canonical_fingerprint(
    *,
    event_id: str,
    subject_id: int,
    purpose: str,
    expected_version: int,  # accepted for call-site symmetry, deliberately
    event_type: EventType,  # NOT part of the hash (see below)
    policy_version: str | None = None,
    expires_at: datetime | None = None,
) -> str:
    """Deterministic hash of the request *content*.

    ``expected_version`` is intentionally excluded: it is an optimistic-
    concurrency precondition ("what the caller believes the stream looks
    like"), not part of what the event says. A retried request with the same
    event id and the same content must replay the original result even if the
    caller refreshed its expected version; a genuinely different request
    reusing an event id differs in subject/purpose/type/policy/expiry and is
    caught here. Stale versions are rejected separately in the locked append.
    """
    _ = expected_version
    payload: dict[str, Any] = {
        "event_id": event_id,
        "subject_id": subject_id,
        "purpose": purpose,
        "event_type": event_type.value,
        "policy_version": policy_version,
        "expires_at": expires_at.astimezone(timezone.utc).isoformat() if expires_at else None,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class FoldEvent:
    """Minimal event shape needed to fold a stream (DB-agnostic)."""

    event_id: str
    event_type: EventType
    version: int
    policy_version: str | None
    expires_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class FoldedState:
    last_event_id: str
    version: int
    grant_event_id: str | None
    withdraw_event_id: str | None
    policy_version: str | None
    expires_at: datetime | None


def fold_stream(events: Sequence[FoldEvent] | Iterable[FoldEvent]) -> FoldedState | None:
    """Reduce one stream's ordered events into its current shape.

    Rules implemented here:

    * the latest event by stream version wins;
    * a grant records the policy version it was explicitly bound to;
    * a withdraw clears the current grant;
    * expiry is **not** a state transition -- validity is evaluated against
      the clock on read, so an expired grant becomes invalid immediately with
      no cleanup job and a late-delivered withdraw cannot resurrect it
      (resurrection requires a brand-new grant event with a new event id).
    """
    ordered = sorted(events, key=lambda e: e.version)
    if not ordered:
        return None

    last = ordered[-1]
    grant_event_id: str | None = None
    withdraw_event_id: str | None = None
    policy_version: str | None = None
    expires_at: datetime | None = None

    if last.event_type is EventType.grant:
        grant_event_id = last.event_id
        policy_version = last.policy_version
        expires_at = last.expires_at
    else:
        withdraw_event_id = last.event_id

    return FoldedState(
        last_event_id=last.event_id,
        version=last.version,
        grant_event_id=grant_event_id,
        withdraw_event_id=withdraw_event_id,
        policy_version=policy_version,
        expires_at=expires_at,
    )


def evaluate_validity(state: FoldedState | None, *, now: datetime | None = None) -> tuple[bool, str]:
    """Translate a folded state into a validity verdict at read time."""
    now = now or utcnow()
    if state is None:
        return False, "no_consent_event"
    if state.withdraw_event_id is not None:
        return False, "withdrawn"
    if state.expires_at is not None:
        expiry = state.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if now >= expiry:
            return False, "expired"
    return True, "valid"
