"""Structured logging helpers.

Every log record is rendered as a single JSON line carrying at least:

* ``ts``        - UTC ISO-8601 timestamp
* ``level``     - log level
* ``event``     - stable machine-readable event name
* ``run_id``    - correlation id for an engine run / API request
* ``version``   - engine version
* arbitrary step fields supplied by the caller

The same records are also captured in-memory per run so the API and the
test-suite can attach "calculation steps and decision basis" to responses and
review logs. Exceptions are logged with ``level=ERROR`` and a structured
``error`` field - they are never folded into a success record.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from .version import __version__

_ROOT_LOGGER_NAME = "reteapp"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="microseconds"
            ),
            "level": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
        }
        extra = getattr(record, "data", None)
        if isinstance(extra, dict):
            for key, value in extra.items():
                payload[key] = value
        if record.exc_info:
            payload["error"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else "Unknown",
                "message": str(record.exc_info[1]),
            }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


_CONFIGURED = False
_CONFIG_LOCK = Lock()


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Idempotently attach one JSON stream handler to the package logger."""

    global _CONFIGURED
    with _CONFIG_LOCK:
        logger = logging.getLogger(_ROOT_LOGGER_NAME)
        logger.setLevel(level)
        logger.propagate = False
        if not _CONFIGURED:
            handler = logging.StreamHandler(stream=sys.stderr)
            handler.setFormatter(JsonFormatter())
            logger.addHandler(handler)
            _CONFIGURED = True
        else:
            for handler in logger.handlers:
                handler.setLevel(level)
        return logger


class RunLogger:
    """Per-run structured logger.

    Every line is emitted through the package's JSON std handler and carries
    ``run_id`` and ``version`` for correlation. The durable, ordered audit
    trail lives in the SQLite evidence store (``trace`` table), which the
    service populates via its recorder; this logger is the human/operator
    counterpart.
    """

    def __init__(self, run_id: str, *, level: str = "INFO") -> None:
        self.run_id = run_id
        self._logger = logging.getLogger(_ROOT_LOGGER_NAME)

    def _emit(self, level: int, event: str, **fields: Any) -> None:
        self._logger.log(
            level,
            event,
            extra={"data": {"run_id": self.run_id, "version": __version__, **fields}},
        )

    def debug(self, event: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit(logging.INFO, event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit(logging.WARNING, event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit(logging.ERROR, event, **fields)
