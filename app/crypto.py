"""Real cryptographic primitives: PBKDF2 password hashing and HMAC tokens.

No placeholder/stub code:
* Passwords use PBKDF2-HMAC-SHA256 with a per-user random salt (240k rounds).
* Session/assignment tokens are ``payload_b64 .signature_b64`` signed with
  HMAC-SHA256 over the server secret; comparison is constant-time.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from . import config


class TokenError(Exception):
    """Raised when a signed token is malformed, expired or tampered with."""


# ---------------------------------------------------------------------------
# Password hashing (PBKDF2-HMAC-SHA256)
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(config.PBKDF2_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, config.PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${config.PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, rounds_s, salt_hex, hash_hex = encoded.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        rounds = int(rounds_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, rounds
    )
    return hmac.compare_digest(candidate, expected)


# ---------------------------------------------------------------------------
# HMAC-SHA256 signed tokens
# ---------------------------------------------------------------------------

def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload_b64: str, secret: str) -> str:
    return _b64encode(
        hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"),
                 hashlib.sha256).digest()
    )


def issue_token(claims: dict, ttl_seconds: int | None = None,
                secret: str | None = None) -> str:
    """Issue an HMAC-signed token carrying JSON claims + issued/expiry times."""
    secret = secret or config.SECRET_KEY
    now = int(time.time())
    body = dict(claims)
    body["iat"] = now
    body["exp"] = now + (
        ttl_seconds if ttl_seconds is not None else config.TOKEN_TTL_SECONDS
    )
    payload_b64 = _b64encode(
        json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = _sign(payload_b64, secret)
    return f"{payload_b64}.{signature}"


def verify_token(token: str, secret: str | None = None) -> dict:
    """Validate signature and expiry, returning the embedded claims."""
    secret = secret or config.SECRET_KEY
    if not token or token.count(".") != 1:
        raise TokenError("malformed token")
    payload_b64, signature = token.split(".", 1)
    expected = _sign(payload_b64, secret)
    if not hmac.compare_digest(signature, expected):
        raise TokenError("invalid signature")
    try:
        claims = json.loads(_b64decode(payload_b64))
    except (ValueError, json.JSONDecodeError) as exc:
        raise TokenError("malformed payload") from exc
    if int(claims.get("exp", 0)) < int(time.time()):
        raise TokenError("token expired")
    return claims
