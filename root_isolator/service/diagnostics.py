"""Structured, redaction-aware diagnostics.

Every log line carries a request id and only safe state: degree, term count and
the non-reversible coefficient fingerprint. Raw coefficient payloads are never
logged when ``redact_polynomials`` is enabled (the default), so a request that
happens to carry sensitive input cannot leak it through logs.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from config.settings import LogConfig

_REDACTED = "<redacted>"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "event", "state", "fingerprint", "category"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(config: LogConfig) -> logging.Logger:
    logger = logging.getLogger("root_isolator")
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(config.level)
    logger.propagate = False
    return logger


class Diagnostics:
    """Request-scoped logger that always knows its request id and policy."""

    def __init__(self, logger: logging.Logger, config: LogConfig, request_id: str) -> None:
        self._logger = logger
        self._config = config
        self.request_id = request_id

    def _emit(self, level: int, event: str, message: str, **state: Any) -> None:
        safe_state = self._redact_state(state)
        self._logger.log(
            level,
            message,
            extra={
                "request_id": self.request_id,
                "event": event,
                "state": safe_state,
            },
        )

    def _redact_state(self, state: dict[str, Any]) -> dict[str, Any]:
        if not self._config.redact_polynomials:
            return state
        redacted: dict[str, Any] = {}
        for key, value in state.items():
            if key in {"coefficients", "coeffs", "polynomial", "raw"}:
                redacted[key] = _REDACTED
            else:
                redacted[key] = value
        return redacted

    def accepted(self, fingerprint: str, **state: Any) -> None:
        self._emit(
            logging.INFO,
            "accepted",
            "isolation request accepted: all exact and numeric evidence agrees",
            fingerprint=fingerprint,
            **state,
        )

    def indeterminate(self, fingerprint: str, reasons: list[str], **state: Any) -> None:
        self._emit(
            logging.WARNING,
            "indeterminate",
            "isolation exact result produced but numeric evidence is inconclusive",
            fingerprint=fingerprint,
            reasons=reasons,
            **state,
        )

    def rejected(self, fingerprint: str, reasons: list[str], **state: Any) -> None:
        self._emit(
            logging.ERROR,
            "rejected",
            "isolation result rejected due to exact-evidence disagreement",
            fingerprint=fingerprint,
            reasons=reasons,
            **state,
        )

    def rejected_request(self, category: str, message: str, **state: Any) -> None:
        self._emit(
            logging.WARNING,
            "request_rejected",
            "request rejected: %s" % message,
            category=category,
            **state,
        )

    def info(self, event: str, message: str, **state: Any) -> None:
        self._emit(logging.INFO, event, message, **state)
