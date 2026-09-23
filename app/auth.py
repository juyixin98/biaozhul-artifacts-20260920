"""
HMAC-SHA256 request authentication for the IK service.

Every mutating request (/api/v1/ik/solve) must be signed. The canonical
string is:

    f"{method}\\n{path}\\n{X-Timestamp}\\n{sha256_hex(raw_body)}"

and the client sends

    X-Key:       <key id>
    X-Timestamp: <unix seconds>
    X-Signature: hex(hmac_sha256(secret, canonical_string))

Key id -> secret pairs are configured with the IK_API_KEYS environment
variable as "id1=secret1,id2=secret2" (default: one development key
"demo-key-id" / "demo-secret", which MUST be replaced in deployment).

Replay protection: timestamps outside +/- IK_TIMESTAMP_SKEW seconds are
rejected. Cryptography is performed with the Python standard library
hmac/hashlib (real constant-time verification via hmac.compare_digest).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

from fastapi import Header, HTTPException, Request

DEFAULT_KEYS = "demo-key-id=demo-secret"
DEFAULT_SKEW = 300.0


def _load_keys() -> dict[str, bytes]:
    raw = os.environ.get("IK_API_KEYS", DEFAULT_KEYS)
    keys: dict[str, bytes] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            continue
        kid, secret = pair.split("=", 1)
        keys[kid.strip()] = secret.strip().encode("utf-8")
    return keys


def _skew() -> float:
    try:
        return float(os.environ.get("IK_TIMESTAMP_SKEW", DEFAULT_SKEW))
    except ValueError:
        return DEFAULT_SKEW


def canonical_string(method: str, path: str, timestamp: str,
                     body: bytes) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method.upper()}\n{path}\n{timestamp}\n{body_hash}".encode("utf-8")


def sign(method: str, path: str, timestamp: str, body: bytes,
         secret: bytes) -> str:
    """Helper used by the client and tests: hex HMAC-SHA256 signature."""
    msg = canonical_string(method, path, timestamp, body)
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


async def require_signature(
    request: Request,
    x_key: str | None = Header(default=None, alias="X-Key"),
    x_timestamp: str | None = Header(default=None, alias="X-Timestamp"),
    x_signature: str | None = Header(default=None, alias="X-Signature"),
) -> None:
    """FastAPI dependency enforcing HMAC-SHA256 signed requests."""
    keys = _load_keys()
    missing = [n for n, v in (("X-Key", x_key),
                              ("X-Timestamp", x_timestamp),
                              ("X-Signature", x_signature)) if not v]
    if missing:
        raise HTTPException(status_code=401,
                            detail=f"missing signature headers: {', '.join(missing)}")

    if x_key not in keys:
        raise HTTPException(status_code=401, detail="unknown key id")

    try:
        ts = float(x_timestamp)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="malformed timestamp")

    if abs(time.time() - ts) > _skew():
        raise HTTPException(status_code=401,
                            detail="timestamp outside allowed skew (replay protection)")

    body = await request.body()
    expected = sign(request.method, request.url.path, x_timestamp, body,
                    keys[x_key])
    if not hmac.compare_digest(expected, x_signature.lower()):
        raise HTTPException(status_code=401, detail="signature mismatch")
