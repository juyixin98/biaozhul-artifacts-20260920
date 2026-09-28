"""Structured diagnostics.

Every audit decision (accept / reject / undefined / indeterminate / invalid)
emits an :class:`AuditRecord` carrying a request id and the *minimal* state
needed to explain the decision.  When redaction is enabled (default), item
names and raw transactions are never logged verbatim: only counts, metric
values, stable hashes and length information leave the boundary.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Sequence

from .models import Itemset, RuleStatus

logger = logging.getLogger("rule_audit")

_counter = itertools.count(1)


def new_request_id() -> str:
    return f"req-{uuid.uuid4().hex[:12]}"


def _stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


def redact_itemset(items: Itemset) -> Dict[str, Any]:
    """Return a non-reversible fingerprint of an itemset."""
    joined = "|".join(items)
    return {
        "size": len(items),
        "itemset_sha256_10": _stable_hash(joined),
    }


@dataclass(frozen=True)
class AuditRecord:
    request_id: str
    stage: str
    status: RuleStatus | str
    message: str
    key_state: Dict[str, Any] = field(default_factory=dict)
    redacted: bool = True

    def to_log_dict(self) -> Dict[str, Any]:
        status = self.status.value if isinstance(self.status, RuleStatus) else str(self.status)
        return {
            "request_id": self.request_id,
            "stage": self.stage,
            "status": status,
            "message": self.message,
            "key_state": self.key_state,
            "redacted": self.redacted,
        }


class DiagnosticCollector:
    """Collects audit records for one request and renders safe summaries."""

    def __init__(self, request_id: str | None = None, redact: bool = True) -> None:
        self.request_id = request_id or new_request_id()
        self.redact = redact
        self._records: list[AuditRecord] = []

    def add(
        self,
        stage: str,
        status: RuleStatus | str,
        message: str,
        **key_state: Any,
    ) -> AuditRecord:
        record = AuditRecord(
            request_id=self.request_id,
            stage=stage,
            status=status,
            message=message,
            key_state=key_state,
            redacted=self.redact,
        )
        self._records.append(record)
        logger.info(
            "[%s] stage=%s status=%s %s state=%s",
            self.request_id,
            stage,
            status,
            message,
            json.dumps(key_state, sort_keys=True, default=str),
        )
        return record

    def itemset_view(self, items: Itemset) -> Dict[str, Any] | list[str]:
        if self.redact:
            return redact_itemset(items)
        return list(items)

    def itemsets_view(self, itemsets: Iterable[Itemset]) -> list[Any]:
        return [self.itemset_view(i) for i in itemsets]

    @property
    def records(self) -> Sequence[AuditRecord]:
        return tuple(self._records)

    def summary(self) -> list[Dict[str, Any]]:
        return [r.to_log_dict() for r in self._records]
