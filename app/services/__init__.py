from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from sqlalchemy.exc import IntegrityError

from ..database import SessionLocal
from ..errors import IdempotencyConflictError
from ..models import IdempotentOp


def normalize_payload(payload: Any) -> bytes:
    """Stable canonical form so semantically identical retries hash equal."""
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def run_idempotent(
    db: SessionLocal,
    key: str | None,
    payload: Any,
    handler: Callable[[], tuple[int, dict]],
) -> tuple[int, dict, bool]:
    """
    Run ``handler`` (which returns (status_code, response_body) and must NOT
    commit) under an idempotency key.

    Same key + same payload  -> original stored result is returned unchanged.
    Same key + different payload -> 409.
    Concurrent first calls with the same key serialise on the primary key.
    """
    if key is None:
        status, body = handler()
        db.commit()
        return status, body, False

    request_hash = hashlib.sha256(normalize_payload(payload)).hexdigest()

    existing = db.get(IdempotentOp, key)
    if existing is not None:
        if existing.request_hash != request_hash:
            raise IdempotencyConflictError(
                "Idempotency-Key was already used with a different request body"
            )
        return existing.status_code, json.loads(existing.response_body), True

    # Claim the key inside a savepoint; a racing first-call loses here.
    try:
        with db.begin_nested():
            db.add(
                IdempotentOp(
                    key=key,
                    request_hash=request_hash,
                    status_code=0,
                    response_body="",
                )
            )
            db.flush()
    except IntegrityError:
        existing = db.get(IdempotentOp, key)
        if existing is None:  # pragma: no cover - impossible under the PK
            raise
        if existing.request_hash != request_hash:
            raise IdempotencyConflictError(
                "Idempotency-Key was already used with a different request body"
            )
        return existing.status_code, json.loads(existing.response_body), True

    # If the handler raises, the request transaction is rolled back by the
    # session dependency, taking the placeholder claim with it.
    status, body = handler()
    op = db.get(IdempotentOp, key)
    op.status_code = status
    op.response_body = json.dumps(body, ensure_ascii=False, default=str)
    db.commit()
    return status, body, False
