"""Structured diagnostics.

Every decision (accepted / rejected / undecidable) is logged with the request
identifier, the record identifiers involved, and the key state that justified
the decision. Document text is treated as sensitive: only its length and a
truncated content fingerprint are ever logged, never the raw text.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

LOGGER_NAME = "bracket_index"

DECISION_ACCEPTED = "accepted"
DECISION_REJECTED = "rejected"
DECISION_UNDECIDABLE = "undecidable"

_FINGERPRINT_CHARS = 12


def configure_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        logger.addHandler(handler)
    logger.setLevel(level.upper())
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def fingerprint(text: str) -> str:
    """Redacted content reference: length plus truncated SHA-256. Never raw text."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:_FINGERPRINT_CHARS]
    return f"len={len(text)} sha256:{digest}"


def log_decision(
    request_id: str,
    decision: str,
    reason: str,
    **state: Any,
) -> None:
    """Emit one structured decision record.

    `state` must only contain non-sensitive key state: ids, versions, offsets,
    counts, categories. Callers must pass fingerprint() output instead of text.
    """
    fields = " ".join(f"{key}={value!r}" for key, value in sorted(state.items()))
    get_logger().info(
        "request_id=%s decision=%s reason=%s%s",
        request_id,
        decision,
        reason,
        f" {fields}" if fields else "",
    )
