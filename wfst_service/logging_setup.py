"""Structured logging configuration.

Every log line is emitted as JSON so it can be correlated with a run id
and carries the service version.  Console logs and per-run database logs
use the same messages.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from . import __version__


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "version": __version__,
            "message": record.getMessage(),
        }
        for key in ("run_id", "corpus_id", "target", "input", "event"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("wfst_service")
    if logger.handlers:
        return logger
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonLineFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger
