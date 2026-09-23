"""Real cryptographic operations for the API.

* Session secrets are generated with :mod:`secrets` (CSPRNG).
* Requests are authenticated with HMAC-SHA256 over a canonical string;
  comparison uses :func:`hmac.compare_digest` (constant time).
* Request bodies are fingerprinted with SHA-256 for replay detection.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

HEADER_SESSION = "X-Session-Id"
HEADER_TS = "X-Timestamp"
HEADER_SIG = "X-Signature"
ALLOWED_SKEW_SECONDS = 300


def new_session_id() -> str:
    return secrets.token_hex(16)


def new_secret() -> str:
    """256-bit URL-safe secret shared out of band with the client."""
    return secrets.token_urlsafe(32)


def canonical_string(method: str, path: str, timestamp: str, body: bytes) -> bytes:
    """Build the exact byte string that is signed.

    Layout (newline separated, no trailing newline)::

        <METHOD>\\n<path>\\n<unix-ts-seconds>\\n<sha256-hex-of-body>
    """
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method.upper()}\n{path}\n{timestamp}\n{body_hash}".encode("utf-8")


def sign(method: str, path: str, timestamp: str, body: bytes, secret: str) -> str:
    msg = canonical_string(method, path, timestamp, body)
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_signature(
    method: str,
    path: str,
    timestamp: str,
    body: bytes,
    secret: str,
    signature_hex: str,
) -> bool:
    expected = sign(method, path, timestamp, body, secret)
    return hmac.compare_digest(expected, signature_hex.strip())


def body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()
