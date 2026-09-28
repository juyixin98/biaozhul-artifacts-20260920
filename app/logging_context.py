"""Request correlation ids and privacy-preserving logging helpers.

Raw observations are *never* logged. Each request is described by stable
metadata (request id, shapes, a truncated hash of the payload, status) so that
diagnostic records explain why a verdict was reached without leaking data.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from contextvars import ContextVar
from typing import Any

current_request_id: ContextVar[str] = ContextVar("current_request_id", default="-")


def new_request_id() -> str:
    return uuid.uuid4().hex


def bind_request_id(request_id: str | None = None) -> str:
    rid = request_id or new_request_id()
    current_request_id.set(rid)
    return rid


class _RequestFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id.get()
        return True


def configure_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("twosls")
    if logger.handlers:
        logger.setLevel(level)
        return logger
    handler = logging.StreamHandler()
    handler.addFilter(_RequestFilter())
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [req=%(request_id)s] %(name)s: %(message)s"
        )
    )
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


def payload_fingerprint(payload: Any, prefix_len: int = 12) -> str:
    """Stable truncated SHA-256 over the JSON-serialized payload.

    Lets logs/records tie a verdict to a dataset without storing the dataset.
    """

    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:prefix_len]


def redacted_summary(columns: dict[str, list[float]], max_preview: int = 0) -> dict:
    """Return non-sensitive metadata describing a payload's columns."""

    summary = {
        "n_columns": len(columns),
        "column_names": sorted(columns.keys()),
        "lengths": {name: len(values) for name, values in columns.items()},
    }
    if max_preview:  # off by default; only the (synthetic) demo ever opts in
        summary["note"] = f"raw preview disabled ({max_preview=})"
    return summary
