"""Diagnostics: request-correlated, state-bearing, sanitized.

Every accepted or rejected decision gets a short structured record carrying
a request id, the outcome, the reason and the key offsets/version that drove
it. Sensitive payload handling: raw document text never enters a record;
only bounded metadata (lengths, offsets, categories) and optionally a short
redacted context snippet are kept.
"""
from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

ACCEPTED = "accepted"
REJECTED = "rejected"
UNDETERMINED = "undetermined"

_MAX_SNIPPET = 24
# Characters that could carry content we must not echo verbatim.
_SENSITIVE_CHARS = set('"\\')


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def redact_snippet(text: str, center: int, radius: int = 8) -> str:
    """Return a tiny, masked context string around ``center``.

    Bracket punctuation is kept (it is the subject of the diagnosis) but any
    other content is collapsed to dots, so a string literal's payload can
    never leak into a log.
    """
    lo = max(0, center - radius)
    hi = min(len(text), center + radius + 1)
    out = []
    for ch in text[lo:hi]:
        if ch in "()[]{}":
            out.append(ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch in _SENSITIVE_CHARS:
            out.append("?")
        else:
            out.append(".")
    snippet = "".join(out)
    if len(snippet) > _MAX_SNIPPET:
        snippet = snippet[:_MAX_SNIPPET] + "…"
    prefix = "…" if lo > 0 else ""
    suffix = "…" if hi < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


@dataclass(frozen=True)
class DiagnosticRecord:
    request_id: str
    outcome: str
    operation: str
    reason: str
    state: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "outcome": self.outcome,
            "operation": self.operation,
            "reason": self.reason,
            "state": self.state,
        }


class DiagnosticLog:
    """In-process ring of diagnostic records plus standard logging."""

    def __init__(self, capacity: int = 512) -> None:
        self._capacity = capacity
        self._records: list[DiagnosticRecord] = []
        self._lock = threading.Lock()
        self._logger = logging.getLogger("bracket_index.diagnostics")

    def record(
        self,
        operation: str,
        outcome: str,
        reason: str,
        state: dict[str, Any],
        request_id: str | None = None,
    ) -> DiagnosticRecord:
        rec = DiagnosticRecord(
            request_id=request_id or new_request_id(),
            outcome=outcome,
            operation=operation,
            reason=reason,
            state=state,
        )
        with self._lock:
            self._records.append(rec)
            if len(self._records) > self._capacity:
                del self._records[: len(self._records) - self._capacity]
        level = {
            ACCEPTED: logging.INFO,
            REJECTED: logging.WARNING,
            UNDETERMINED: logging.WARNING,
        }[outcome]
        # State contains only offsets/versions/counts; snippets are redacted.
        self._logger.log(
            level,
            "req=%s op=%s outcome=%s reason=%s state=%s",
            rec.request_id,
            operation,
            outcome,
            reason,
            rec.state,
        )
        return rec

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [r.as_dict() for r in self._records[-limit:]]
