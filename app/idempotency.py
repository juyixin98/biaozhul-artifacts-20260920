"""Idempotency-key handling backed by PostgreSQL advisory locks.

Concurrency design
------------------
For every mutating request carrying ``X-Request-Id``:

1. Take a transaction-scoped advisory lock derived from the request id.  Two
   concurrent requests with the same id therefore serialise on the database;
   the winner inserts :class:`IdempotencyRecord`, the loser sees the stored
   result.
2. After the business logic succeeds, store ``(status_code, body)``.  It is
   committed in the *same* transaction as the state/task/audit mutations, so a
   stored response always implies the effect happened exactly once.
3. A replay returns the stored body with the same status code.  A request that
   reuses an id but targets a different instance/action gets 409 and writes
   nothing.
"""
from __future__ import annotations

import hashlib

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import conflict
from app.models import IdempotencyRecord

LOCK_SALT = b"workflow-engine-idem-v1\x00"


async def acquire_request_lock(session: AsyncSession, request_id: str) -> None:
    """Serialise concurrent requests sharing a request id."""
    await _acquire_lock(session, LOCK_SALT + request_id.encode())


_TEMPLATE_SALT = b"workflow-engine-template-code-v1\x00"


async def acquire_template_lock(session: AsyncSession, template_code: str) -> None:
    """Serialise version allocation for one template code.

    Locking an empty result set with ``FOR UPDATE`` does not block two
    concurrent first publishes, so version numbers are allocated under this
    stable per-code advisory lock instead.
    """
    await _acquire_lock(session, _TEMPLATE_SALT + template_code.encode())


async def _acquire_lock(session: AsyncSession, payload: bytes) -> None:
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    key = int.from_bytes(digest, "big", signed=True)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": key}
    )


class Replayed(Exception):
    """Raised when the request is a replay; carries the stored response."""

    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self.body = body
        super().__init__("idempotent replay")


async def begin_idempotent(
    session: AsyncSession,
    request_id: str,
    method: str,
    instance_id: int | None,
) -> None:
    """Lock and check a request id. Raises :class:`Replayed` on a duplicate.

    For ``start``/``publish``/``rollback`` the target instance does not exist
    (or is irrelevant), so a replay is identified by the scoped method (e.g.
    ``start:expense``, ``publish:expense``) alone; for decisions and withdrawal
    the stored instance id must also match the one in the URL.
    """
    await acquire_request_lock(session, request_id)
    record = await session.get(IdempotencyRecord, request_id)
    if record is not None:
        if method.startswith(("start:", "publish:", "rollback:")):
            same_target = record.method == method
        else:
            same_target = (
                record.instance_id == instance_id and record.method == method
            )
        if not same_target:
            raise conflict(
                "request_id_conflict",
                "request id was already used for a different action or instance",
            )
        raise Replayed(record.status_code, record.response_body)


async def store_idempotent_result(
    session: AsyncSession,
    request_id: str,
    method: str,
    instance_id: int | None,
    status_code: int,
    body: dict,
) -> None:
    session.add(
        IdempotencyRecord(
            request_id=request_id,
            instance_id=instance_id,
            method=method,
            status_code=status_code,
            response_body=body,
        )
    )
