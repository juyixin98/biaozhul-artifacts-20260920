"""Stateless HMAC-signed bearer tokens.

Device tokens identify a device; session tokens identify a lease and carry
the lease generation. Revoked devices and superseded generations are rejected
because every authenticated request is re-validated against the database.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

from app.config import get_settings


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(payload_b64: str, secret: str) -> str:
    return _b64encode(hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest())


def issue_token(kind: str, subject_id: int, generation: int = 0) -> str:
    payload = {"k": kind, "id": subject_id, "g": generation}
    payload_b64 = _b64encode(json.dumps(payload, separators=(",", ":")).encode())
    return f"{payload_b64}.{_sign(payload_b64, get_settings().token_secret)}"


def decode_token(token: str) -> dict | None:
    try:
        payload_b64, sig = token.split(".", 1)
    except (ValueError, AttributeError):
        return None
    if not hmac.compare_digest(sig, _sign(payload_b64, get_settings().token_secret)):
        return None
    try:
        data = json.loads(_b64decode(payload_b64))
    except Exception:
        return None
    if not isinstance(data, dict) or "k" not in data or "id" not in data:
        return None
    return data


def issue_device_token(device_id: int) -> str:
    return issue_token("device", device_id)


def issue_session_token(lease_id: int, generation: int) -> str:
    return issue_token("session", lease_id, generation)


def hash_admin_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()
