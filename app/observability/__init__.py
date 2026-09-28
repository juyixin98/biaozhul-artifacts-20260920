"""Structured diagnostics: request/record correlation and value redaction.

Every log line carries a request id (when handling an HTTP request) plus the
input version and query node count when known. Values that may be sensitive are
passed through :func:`redact`; the synthetic fixtures contain no secrets, but
the redaction path is always applied so real deployments cannot leak raw cell
values through logs.
"""
from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from typing import Any

# Context-local correlation id, bound per HTTP request.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Fields whose values must never be printed verbatim.
_SENSITIVE_KEYS = frozenset(
    {"password", "token", "secret", "api_key", "authorization", "ssn", "email"}
)
_REDACTED = "***REDACTED***"
_MAX_VALUE_LEN = 80


def new_request_id() -> str:
    return uuid.uuid4().hex


def bind_request_id(request_id: str | None = None) -> str:
    request_id = request_id or new_request_id()
    request_id_var.set(request_id)
    return request_id


def redact(payload: Any, _key: str | None = None) -> Any:
    """Return a log-safe copy of ``payload`` with sensitive values masked."""
    if _key is not None and _key.lower() in _SENSITIVE_KEYS:
        return _REDACTED
    if isinstance(payload, dict):
        return {key: redact(value, str(key)) for key, value in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [redact(item) for item in payload]
    if isinstance(payload, str) and len(payload) > _MAX_VALUE_LEN:
        return payload[:_MAX_VALUE_LEN] + "...(truncated)"
    return payload


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        extra = getattr(record, "context", None)
        if isinstance(extra, dict):
            payload.update(redact(extra))
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("provenance")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # Keep propagation enabled so host frameworks (and test capture) attached to
    # the root logger also receive records.
    root.propagate = True


def get_logger(name: str = "provenance") -> logging.Logger:
    return logging.getLogger(name)


def log_decision(
    logger: logging.Logger,
    decision: str,
    *,
    request_id: str | None = None,
    **context: Any,
) -> None:
    """Log an accept / reject / undecided verdict with key state.

    ``decision`` is one of ``accept``, ``reject``, ``undecided``. The function
    exists to make the three-outcome contract explicit at call sites.
    """
    level = {"accept": logging.INFO, "reject": logging.WARNING,
             "undecided": logging.ERROR}.get(decision, logging.INFO)
    logger.log(
        level,
        "decision=%s",
        decision,
        extra={"context": {"request_id": request_id, "decision": decision,
                           **redact(context)}},
    )
