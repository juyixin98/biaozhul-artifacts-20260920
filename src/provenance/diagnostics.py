"""Structured diagnostics with a request/record id and secret redaction.

Every decision the service makes (accept / reject / indeterminable) is emitted
as one JSON line carrying:

* ``request_id``  – ties an API call to all of its diagnostics,
* ``record_id``   – identifies the input/output tuple when applicable,
* ``state``       – key state at the decision point,
* ``outcome``     – one of ``accepted`` / ``rejected`` / ``indeterminable``.

Values whose key looks sensitive are rendered redacted; raw secrets are never
written to the log.
"""
from __future__ import annotations

import json
import logging
import sys
import uuid
from dataclasses import dataclass
from typing import Any

_SENSITIVE_PARTS = ("password", "passwd", "secret", "token", "apikey", "api_key", "authorization", "ssn")
REDACTED = "***REDACTED***"


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_PARTS)


def redact(value: Any, _depth: int = 0) -> Any:
    """Return a copy of ``value`` with sensitive leaf values masked."""
    if _depth > 6:
        return "..."
    if isinstance(value, dict):
        return {
            k: (REDACTED if _is_sensitive(str(k)) else redact(v, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in value]
    return value


@dataclass(frozen=True)
class Decision:
    outcome: str  # accepted | rejected | indeterminable
    reason: str
    state: dict[str, Any]


class JsonDiagnostics:
    """Emits one JSON object per line; safe for both API and library use."""

    def __init__(self, logger_name: str = "provenance", *, stream=None):
        # A unique child logger per instance keeps each diagnostics sink
        # isolated (e.g. one StringIO per test) instead of inheriting a
        # handler attached to a previously created same-named logger.
        unique_name = f"{logger_name}.{uuid.uuid4().hex[:8]}"
        self._logger = logging.getLogger(unique_name)
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(handler)
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False

    def emit(
        self,
        decision: Decision,
        *,
        request_id: str,
        record_id: str | None = None,
    ) -> None:
        payload = {
            "request_id": request_id,
            "record_id": record_id,
            "outcome": decision.outcome,
            "reason": decision.reason,
            "state": redact(decision.state),
        }
        level = logging.INFO if decision.outcome == "accepted" else logging.WARNING
        self._logger.log(level, json.dumps(payload, ensure_ascii=False, sort_keys=True))

    # Convenience constructors making the "why accepted/rejected" explicit.
    def accepted(self, reason: str, state: dict[str, Any], *, request_id: str, record_id: str | None = None) -> None:
        self.emit(Decision("accepted", reason, state), request_id=request_id, record_id=record_id)

    def rejected(self, reason: str, state: dict[str, Any], *, request_id: str, record_id: str | None = None) -> None:
        self.emit(Decision("rejected", reason, state), request_id=request_id, record_id=record_id)

    def indeterminable(self, reason: str, state: dict[str, Any], *, request_id: str, record_id: str | None = None) -> None:
        self.emit(Decision("indeterminable", reason, state), request_id=request_id, record_id=record_id)
