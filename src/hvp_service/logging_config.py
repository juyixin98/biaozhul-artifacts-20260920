"""Structured JSON logging.

Every log record carries the event name and any structured fields (run_id,
node counts, decision reasons) as JSON so test logs and production logs can
be replayed: a failing run_id plus the logged intermediate states is enough
to reconstruct what the service decided and why.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

_LOGGER_NAME = "hvp_service"
_configured = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def get_logger() -> logging.Logger:
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    if not _configured:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = True  # let pytest caplog see records
        _configured = True
    return logger


def log_event(level: int, event: str, **fields) -> None:
    get_logger().log(level, event, extra={"fields": {"event": event, **fields}})
