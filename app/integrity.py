"""Integrity helpers — real SHA-256 digests, never placeholders."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json_bytes(obj: Any) -> bytes:
    """Deterministic JSON encoding used for hashing and run records."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def sha256_of(obj: Any) -> str:
    payload = obj if isinstance(obj, (bytes, bytearray)) else canonical_json_bytes(obj)
    return hashlib.sha256(payload).hexdigest()
