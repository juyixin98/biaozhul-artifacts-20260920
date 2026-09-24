"""Structured JSON audit logging.

Hard rule: the raw token never appears in a log line. Tokens are identified
by a short SHA-256 fingerprint, and claim values are never logged either —
only the issuer id, kid, alg and the structured outcome.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("jwt_gateway")
    logger.setLevel(level)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def audit_fields(
    *,
    outcome: str,
    issuer_id: str | None,
    kid: str | None,
    alg: str | None,
    fingerprint: str,
    error_code: str | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "event": "token_verification",
        "outcome": outcome,
        "token_fingerprint": fingerprint,
    }
    if issuer_id is not None:
        fields["issuer_id"] = issuer_id
    if kid is not None:
        fields["kid"] = kid
    if alg is not None:
        fields["alg"] = alg
    if error_code is not None:
        fields["error_code"] = error_code
    return fields
