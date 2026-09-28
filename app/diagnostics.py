"""Structured diagnostics with request/record identifiers and masking.

Every accept / reject / undecidable decision in the pipeline is logged with:
  * a request id (propagated from the HTTP layer or generated per call),
  * the record id it concerns (corpus, transaction, rule),
  * the key state that drove the decision.

Item values may be sensitive, so logs only carry a truncated hash of each
item unless masking is explicitly disabled in settings.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from contextvars import ContextVar

_request_id: ContextVar[str] = ContextVar("request_id", default="-")

logger = logging.getLogger("audit")


def new_request_id() -> str:
    """Generate and bind a fresh request id to the current context."""
    rid = uuid.uuid4().hex[:12]
    _request_id.set(rid)
    return rid


def set_request_id(rid: str) -> None:
    _request_id.set(rid)


def get_request_id() -> str:
    return _request_id.get()


def mask_item(item: str, *, enabled: bool = True) -> str:
    """Return a log-safe representation of an item value.

    The raw item is never logged when masking is enabled; a 12-char
    SHA-256 prefix keeps values distinguishable without revealing them.
    """
    if not enabled:
        return item
    digest = hashlib.sha256(item.encode("utf-8")).hexdigest()[:12]
    return f"item#{digest}"


def mask_items(items: list[str] | frozenset[str] | set[str], *, enabled: bool = True) -> list[str]:
    return sorted(mask_item(i, enabled=enabled) for i in items)


def log_decision(
    decision: str,
    *,
    reason: str,
    record_id: str | None = None,
    state: dict | None = None,
) -> None:
    """Emit one structured decision record.

    Args:
        decision: one of ``accepted`` / ``rejected`` / ``undecidable``.
        reason: machine-readable category (e.g. ``EMPTY_ANTECEDENT``).
        record_id: corpus/transaction/rule identifier, if any.
        state: key scalar state (counts, thresholds). Must already be
            masked if it references item values.
    """
    logger.info(
        "decision=%s reason=%s request_id=%s record_id=%s state=%s",
        decision,
        reason,
        get_request_id(),
        record_id or "-",
        state or {},
    )
