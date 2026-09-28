"""Structured, request-correlated logging.

Every log line is JSON and carries the request id, service version and
location; failures and uncertain conclusions use their own fields so they can
be filtered independently of ordinary step logs.
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any

from ..config import SERVICE_NAME, SERVICE_VERSION


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "message": record.getMessage(),
        }
        for key in ("request_id", "location", "failure_category", "uncertain",
                    "graph_id", "state_id", "steps"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("tensor_backend")
    if logger.handlers:
        return logger
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


def log_event(
    logger: logging.Logger,
    message: str,
    *,
    request_id: str | None = None,
    location: str | None = None,
    level: int = logging.INFO,
    failure_category: str | None = None,
    uncertain: Any = None,
    **extra: Any,
) -> None:
    logger.log(
        level,
        message,
        extra={
            "request_id": request_id,
            "location": location,
            "failure_category": failure_category,
            "uncertain": uncertain,
            **extra,
        },
    )
