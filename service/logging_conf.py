"""Logging and redaction.

Diagnostics must be reproducible: every failure log carries the request id and
the decision category. Payloads are never logged; error context passes
through :func:`redact_context`, which keeps only small scalars and shapes.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

# Shapes/bounds are small integer sequences; anything longer is treated as a
# potential payload and redacted.
_MAX_SHAPE_LENGTH = 8

LOGGER_NAME = "qengine"


class _RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = getattr(record, "request_id", "-")
        record.context = getattr(record, "context", {})
        return True


def configure_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s "
                "[%(request_id)s] %(message)s %(context)s"
            )
        )
        handler.addFilter(_RequestContextFilter())
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


def redact_context(context: dict[str, Any]) -> dict[str, Any]:
    """Return a log-safe copy of error context.

    Policy:
      * scalars (int/float/bool/str, incl. numpy scalars) pass through;
      * small integer sequences pass through as shapes/lengths;
      * everything else (arrays, payloads, nested structures) is replaced by
        a type tag, so a tensor can never land in a log line.
    """
    safe: dict[str, Any] = {}
    for key, value in context.items():
        if isinstance(value, (bool, np.bool_)):
            safe[key] = bool(value)
        elif isinstance(value, (int, np.integer)):
            safe[key] = int(value)
        elif isinstance(value, (float, np.floating)):
            safe[key] = float(value)
        elif isinstance(value, str) and len(value) <= 64:
            safe[key] = value
        elif isinstance(value, (tuple, list)) and len(value) <= _MAX_SHAPE_LENGTH:
            if all(
                isinstance(
                    v, (int, np.integer, float, np.floating, bool, np.bool_, str)
                )
                for v in value
            ):
                # Short lists of plain scalars are diagnostic metadata
                # (shapes, available model ids); never tensor payloads.
                safe[key] = [_to_plain_scalar(v) for v in value]
            else:
                safe[key] = f"<redacted:{type(value).__name__}>"
        else:
            safe[key] = f"<redacted:{type(value).__name__}>"
    return safe


def _to_plain_scalar(value: Any) -> Any:
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value)
    return value


def log_failure(
    logger: logging.Logger,
    *,
    request_id: str,
    code: str,
    category: str,
    message: str,
    context: dict[str, Any],
) -> None:
    extra = {
        "request_id": request_id,
        "context": {"code": code, "category": category, **redact_context(context)},
    }
    logger.warning("%s: %s", code, message, extra=extra)
