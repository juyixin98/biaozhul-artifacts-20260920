"""Request-scoped diagnostics: correlation ids, structured logs, redaction.

Every response and every log line carries the same ``request_id`` so an
"accepted / rejected / inconclusive" decision can be traced end to end.
Raw input values are *never* logged - only counts and sign/magnitude
buckets - because input data may be sensitive.
"""
from __future__ import annotations

import contextvars
import json
import logging
import math
import sys
import uuid
from typing import Any

import numpy as np

#: Correlation id of the request currently being handled.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)

_LOGGER_NAME = "summation_compare"


def new_request_id() -> str:
    """Short, unambiguous correlation id."""
    return uuid.uuid4().hex[:16]


class JsonLineFormatter(logging.Formatter):
    """One JSON object per log line, stable key order."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", request_id_var.get()),
            "event": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload["fields"] = extra
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # default=str keeps the log line intact even if a caller passes an
        # exotic object; logging must never itself raise.
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    """Install the JSON-line handler (idempotent)."""
    logger = logging.getLogger(_LOGGER_NAME)
    if not any(getattr(h, "_sumapi_json", False) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonLineFormatter())
        handler._sumapi_json = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


def log_event(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit a structured record. Values must already be non-sensitive."""
    logger = logging.getLogger(_LOGGER_NAME)
    logger.log(level, event, extra={"request_id": request_id_var.get(), "fields": fields})


# ---------------------------------------------------------------------------
# Redaction helpers.
# ---------------------------------------------------------------------------

def magnitude_bucket(value: float) -> str:
    """Map a finite float to a sign + decimal-order-of-magnitude bucket.

    ``-37251.0`` -> ``"-1e4"``; ``0.0`` / ``-0.0`` -> ``"+0"`` / ``"-0"``.
    The bucket conveys scale for diagnostics without revealing the datum.
    """
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+inf" if value > 0 else "-inf"
    if value == 0.0:
        return "-0" if math.copysign(1.0, value) < 0 else "+0"
    sign = "-" if value < 0 else "+"
    return f"{sign}1e{math.floor(math.log10(abs(value)))}"


def summarize_input(values: np.ndarray, sample_buckets: int = 3) -> dict[str, Any]:
    """Return a safe-to-log census of the input array.

    Contains counts and magnitude buckets of the first/last few elements,
    never the raw values.
    """
    finite = np.isfinite(values)
    n_inf = int(np.isinf(values).sum())
    n_nan = int(values.size - finite.sum() - n_inf)
    finite_abs = np.abs(values[finite])
    head = values[:sample_buckets].tolist()
    tail = values[-sample_buckets:].tolist() if values.size > sample_buckets else []
    return {
        "n": int(values.size),
        "n_finite": int(finite.sum()),
        "n_nan": n_nan,
        "n_inf": n_inf,
        # Only order-of-magnitude buckets, never the raw extrema.
        "finite_abs_min_bucket": magnitude_bucket(float(finite_abs.min())) if finite_abs.size else None,
        "finite_abs_max_bucket": magnitude_bucket(float(finite_abs.max())) if finite_abs.size else None,
        "redacted": True,
        "head_magnitude_buckets": [magnitude_bucket(float(v)) for v in head],
        "tail_magnitude_buckets": [magnitude_bucket(float(v)) for v in tail],
    }
