"""Structured JSON-lines logging correlated by request_id.

Every log line is a single JSON object carrying at least ``ts``, ``level``,
``event`` and ``request_id`` so a request can be traced end to end and failures
are greppable by ``failure_category``. Key processing steps emitted by the
service are logged at INFO; rejections at WARNING, with the reason separated
from any statistical uncertainty.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key in (
            "request_id",
            "endpoint",
            "status",
            "failure_category",
            "core_version",
            "detail",
            "n_obs",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logger(path: str | Path, level: str = "INFO") -> logging.Logger:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("did_service")
    logger.setLevel(level)
    logger.propagate = False
    # Avoid duplicate handlers on reload/re-import.
    logger.handlers.clear()

    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(JsonLineFormatter())
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(JsonLineFormatter())
    logger.addHandler(stream_handler)
    return logger


def log(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    logger.log(level, event, extra=fields)
