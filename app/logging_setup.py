"""Structured JSON logging.

Every mining log record carries the run identity (run_id), the computation
step, and the decision basis (support vs min_support) so test logs and
server logs can be traced back to a concrete input.
"""

from __future__ import annotations

import json
import logging

EXTRA_KEYS = (
    "run_id",
    "corpus_id",
    "corpus_digest",
    "step",
    "pattern",
    "support",
    "min_support",
    "decision",
    "detail",
    "versions",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in EXTRA_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("fspm")
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"fspm.{name}")
