"""Structured JSON logging.

Every log line is a JSON object carrying the request identity, the service
version, the processing location and the step. Failure reasons and uncertain
(assumption-dependent) conclusions are logged under dedicated fields so they
remain easy to distinguish from the happy path.
"""
from __future__ import annotations

import json
import logging
import platform
import sys
import time

from . import __version__
from .config import SETTINGS

_HOST = platform.node() or "local"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": SETTINGS.service_name,
            "version": __version__,
            "host": _HOST,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (
            "request_id",
            "endpoint",
            "step",
            "status",
            "failures",
            "uncertainties",
            "excluded_object_ids",
            "run_id",
            "elapsed_ms",
        ):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("did_service")
    if logger.handlers:
        return logger
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(SETTINGS.log_level)
    logger.propagate = False
    return logger


LOGGER = configure_logging()
