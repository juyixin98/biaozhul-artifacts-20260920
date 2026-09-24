"""Real cryptographic primitives: HMAC-SHA256 signatures and replay defence.

Keys are 32-byte urlsafe-base64 strings (see ``scripts/gen_keys.py``).
Signatures use canonical JSON (sorted keys, no whitespace) so that the same
JSON object serialises identically on the client, gateway and robot no matter
the original member ordering.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import threading
import time
from typing import Any

NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
DEFAULT_TIMESTAMP_WINDOW = 60.0  # seconds


class CryptoError(ValueError):
    pass


def b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64decode(data: str) -> bytes:
    if not isinstance(data, str):
        raise CryptoError("key must be a string")
    padding = "=" * ((4 - len(data) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(data + padding)
    except Exception as exc:  # noqa: BLE001
        raise CryptoError("invalid base64") from exc
    if len(raw) != 32:
        raise CryptoError(f"key must decode to 32 bytes, got {len(raw)}")
    return raw


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON encoding used as the signed payload."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def hmac_sign(payload: bytes, key_b64: str) -> str:
    key = b64decode(key_b64)
    return b64encode(hmac.new(key, payload, hashlib.sha256).digest())


def hmac_verify(payload: bytes, signature: str, key_b64: str) -> bool:
    if not isinstance(signature, str):
        return False
    try:
        expected = hmac_sign(payload, key_b64)
    except CryptoError:
        return False
    return hmac.compare_digest(expected, signature)


def request_signing_payload(
    method: str,
    path: str,
    tester: str,
    nonce: str,
    timestamp: float,
    body: Any,
) -> bytes:
    """Canonical bytes covered by an HTTP request signature.

    ``body`` is the *parsed* JSON value (or ``None`` for bodyless requests);
    it is re-canonicalised so member order does not matter.
    """
    body_can = canonical_json(body) if body is not None else b""
    return canonical_json(
        {
            "m": method.upper(),
            "p": path,
            "t": tester,
            "n": nonce,
            "ts": round(float(timestamp), 6),
            "bh": hashlib.sha256(body_can).hexdigest(),
        }
    )


class NonceStore:
    """Remembers used nonces within the timestamp window (replay defence)."""

    def __init__(self, window: float = DEFAULT_TIMESTAMP_WINDOW) -> None:
        self._window = window
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def check(self, nonce: str, timestamp: float, now: float | None = None) -> tuple[bool, str | None]:
        now = time.time() if now is None else now
        if not isinstance(nonce, str) or not NONCE_RE.fullmatch(nonce):
            return False, "bad_nonce"
        if abs(now - float(timestamp)) > self._window:
            return False, "stale_timestamp"
        with self._lock:
            # Evict by the presented timestamp: any live nonce sits inside the
            # window, and the skew check above rejects timestamps outside it.
            cutoff = float(timestamp) - 2 * self._window
            self._seen = {n: ts for n, ts in self._seen.items() if ts >= cutoff}
            if nonce in self._seen:
                return False, "replayed_nonce"
            self._seen[nonce] = float(timestamp)
        return True, None
