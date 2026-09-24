"""Canonical encoding.

All hashing and signing goes through this single function so that the
producer (server) and the verifier always byte-serialize objects identically:

* dict keys sorted recursively (json.dumps(sort_keys=True) sorts nested keys)
* no insignificant whitespace
* UTF-8, non-ASCII emitted as-is (ensure_ascii=False)

The input MUST be JSON data composed only of dict/list/str/int/float/bool/None.
Bytes are forbidden (not representable canonically without an encoding tag);
callers base64-encode binary data first.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

GENESIS_HASH = "0" * 64
GENESIS_CHECKPOINT_HASH = "0" * 64


def canonical(obj: Any) -> bytes:
    """Deterministic byte encoding of a JSON-compatible object."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_canonical(obj: Any) -> str:
    """SHA-256 hex digest of the canonical encoding of obj."""
    return sha256_hex(canonical(obj))
