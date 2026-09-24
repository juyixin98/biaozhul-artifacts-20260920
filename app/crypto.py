"""Real cryptographic operations for snapshot binding and response signing.

Nothing here is a placeholder:

* :func:`canonical_snapshot_payload` builds a deterministic JSON byte string
  from the full map state (shape, every cost, every blocked flag,
  connectivity and corner rule, goal).
* :func:`snapshot_digest` hashes it with SHA-256 (via :mod:`hashlib`).
* :func:`new_hmac_key` generates 32 random bytes with :mod:`secrets`.
* :func:`sign_response` / :func:`verify_signature` use HMAC-SHA256
  (:func:`hmac.new`, :func:`hmac.compare_digest`) so clients can verify a
  planning response really came from the service and was not tampered with.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Any, Iterable

__all__ = [
    "canonical_snapshot_payload",
    "snapshot_digest",
    "new_hmac_key",
    "key_hex",
    "sign_response",
    "verify_signature",
]


def canonical_snapshot_payload(
    *,
    width: int,
    height: int,
    cost: Any,
    blocked: Any,
    connectivity: int,
    diagonal_rule: str,
    start: tuple[int, int],
    goal: tuple[int, int],
) -> bytes:
    """Deterministic byte serialization of everything that defines a planning snapshot.

    The snapshot binds the *map* (shape, every cost, every blocked flag,
    connectivity and corner rule) *and* the query frame (start, goal): a
    path computed under the old start is invalid after moving it, so the
    start is part of the digest. Costs are converted to fixed-precision
    decimal strings so the digest is stable across JSON round-trips (``1``
    vs ``1.0``) and NumPy types.
    """
    flat_cost = [f"{float(v):.10g}" for v in _iter_floats(cost, width * height)]
    flat_blocked = [bool(v) for v in _iter_floats(blocked, width * height)]
    doc = {
        "version": 1,
        "width": int(width),
        "height": int(height),
        "connectivity": int(connectivity),
        "diagonal_rule": str(diagonal_rule),
        "start": [int(start[0]), int(start[1])],
        "goal": [int(goal[0]), int(goal[1])],
        "cost_row_major": flat_cost,
        "blocked_row_major": flat_blocked,
    }
    # sort_keys + no whitespace + trailing newline: canonical form.
    body = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return (body + "\n").encode("ascii")


def _iter_floats(values: Any, expected: int) -> Iterable[float]:
    # Accept plain Python nested lists or NumPy arrays alike.
    if hasattr(values, "reshape"):
        flat = values.reshape(-1).tolist()
    elif isinstance(values, list) and values and isinstance(values[0], list):
        flat = [v for row in values for v in row]
    else:
        flat = list(values)
    if len(flat) != expected:
        raise ValueError("snapshot input has wrong number of cells")
    return flat


def snapshot_digest(payload: bytes) -> str:
    """SHA-256 hex digest of the canonical snapshot payload."""
    return hashlib.sha256(payload).hexdigest()


def new_hmac_key() -> bytes:
    """32 cryptographically secure random bytes (HMAC-SHA256 key)."""
    return secrets.token_bytes(32)


def key_hex(key: bytes) -> str:
    return key.hex()


def sign_response(key: bytes, response_json_bytes: bytes, snapshot_id: str) -> str:
    """HMAC-SHA256 over ``snapshot_id || '.' || canonical response bytes``."""
    msg = snapshot_id.encode("ascii") + b"." + response_json_bytes
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def verify_signature(key: bytes, response_json_bytes: bytes, snapshot_id: str, signature: str) -> bool:
    """Constant-time signature verification."""
    expected = sign_response(key, response_json_bytes, snapshot_id)
    return hmac.compare_digest(expected, str(signature))
