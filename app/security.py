"""Real cryptographic primitives used to bind a plan token to its contents.

A token is ``<base64url(payload)>.<base64url(HMAC-SHA256(secret, payload))>``.
The payload covers the drain id, the plan generation, the step index and the
snapshot tick, so a token is invalidated by:

* a snapshot generation change (external mutation / re-plan),
* advancing to another step,
* tampering with any covered field.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any


def new_secret(num_bytes: int = 32) -> str:
    """Return a fresh cryptographically random secret (hex encoded)."""
    return os.urandom(num_bytes).hex()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64url(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def sign_token(secret: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return f"{_b64url(body)}.{_b64url(mac)}"


class TokenError(Exception):
    pass


def verify_token(secret: str, token: str) -> dict[str, Any]:
    """Verify signature and return the payload. Raises TokenError otherwise."""
    parts = token.split(".")
    if len(parts) != 2:
        raise TokenError("malformed token")
    body_b64, sig_b64 = parts
    try:
        body = _unb64url(body_b64)
        sig = _unb64url(sig_b64)
    except Exception as exc:  # noqa: BLE001 - all decode errors => invalid token
        raise TokenError("malformed token encoding") from exc
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise TokenError("signature mismatch")
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise TokenError("malformed token payload") from exc
    if not isinstance(payload, dict):
        raise TokenError("token payload is not an object")
    return payload


def sha256_fingerprint(obj: Any) -> str:
    """Deterministic SHA-256 fingerprint of a JSON-serialisable object."""
    body = json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str).encode()
    return hashlib.sha256(body).hexdigest()
