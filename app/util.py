"""Canonical serialization + hashing helpers shared by params, evidence chain."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_bytes(obj: Any) -> bytes:
    """Deterministic JSON encoding (sorted keys, tight separators, ASCII)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def sha256_hex(obj: Any) -> str:
    """SHA-256 over the canonical encoding of ``obj``."""
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()
