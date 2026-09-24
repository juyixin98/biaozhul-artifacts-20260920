"""Cryptographic integrity helpers (real crypto, executed on every request).

* ``request_sha256``  — SHA-256 of the exact raw request bytes the client sent.
* ``response_sha256`` — SHA-256 of the canonical JSON serialization of the
  response payload (the ``integrity`` field itself excluded).
* ``hmac_sha256``     — keyed HMAC-SHA256 of the same canonical payload, only
  emitted when the ``TRAJECTORY_EVAL_HMAC_KEY`` environment variable is set, so
  clients can authenticate that a response was produced by this server.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

HMAC_ENV_VAR = "TRAJECTORY_EVAL_HMAC_KEY"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(payload: Any) -> bytes:
    """Deterministic JSON encoding shared by the checksum and the HMAC."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def response_hash(payload: Any) -> str:
    return sha256_hex(canonical_json(payload))


def maybe_hmac(payload: Any, key: str | None = None) -> str | None:
    key = key if key is not None else os.environ.get(HMAC_ENV_VAR)
    if not key:
        return None
    return hmac.new(key.encode("utf-8"), canonical_json(payload), hashlib.sha256).hexdigest()
