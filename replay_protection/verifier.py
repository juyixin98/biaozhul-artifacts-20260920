"""High-level request verification: header parsing, window, HMAC, nonce."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .keys import KeyRegistry
from .nonce_store import NonceStore
from .signing import (
    SIGNING_ALGORITHM,
    canonical_request,
    verify_signature,
)

DEFAULT_WINDOW = 300  # seconds; |now - timestamp| <= window (boundaries included)
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
AUTH_RE = re.compile(
    r"^HMAC-SHA256\s+Credential=(?P<kid>[!\x23-\x7E]+?),\s*Signature=(?P<sig>[0-9a-fA-F]{64})$"
)
KID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")
TIMESTAMP_RE = re.compile(r"^[0-9]{1,11}$")


class VerificationError(Exception):
    """Verification failure. ``code`` is a stable machine-readable string."""

    def __init__(self, code: str, http_status: int, message: str):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message


@dataclass
class AuthenticatedRequest:
    kid: str
    nonce: str
    timestamp: int


class RequestVerifier:
    def __init__(
        self,
        registry: KeyRegistry,
        nonces: NonceStore,
        window_seconds: int = DEFAULT_WINDOW,
        clock=time.time,
    ):
        self.registry = registry
        self.nonces = nonces
        self.window = window_seconds
        self._clock = clock

    def verify(
        self,
        method: str,
        target: str,
        body: bytes,
        headers: dict[str, str],
    ) -> AuthenticatedRequest:
        """Run all checks. Raises VerificationError on the first failure.

        Order matters: malformed input -> unknown key -> stale timestamp ->
        bad signature -> replay. Invalid signatures never consume a nonce.
        """
        kid = headers.get("X-Key-Id", "").strip()
        ts_raw = headers.get("X-Timestamp", "").strip()
        nonce = headers.get("X-Nonce", "").strip()
        auth = headers.get("Authorization", "").strip()

        if not kid or not ts_raw or not nonce or not auth:
            raise VerificationError(
                "missing_headers", 401, "missing one of X-Key-Id/X-Timestamp/X-Nonce/Authorization"
            )
        if not KID_RE.match(kid):
            raise VerificationError("invalid_key_id", 401, "malformed X-Key-Id")
        if not TIMESTAMP_RE.match(ts_raw):
            raise VerificationError("invalid_timestamp", 401, "X-Timestamp must be unix seconds")
        if not NONCE_RE.match(nonce):
            raise VerificationError(
                "invalid_nonce", 401, "X-Nonce must be 16-128 chars from [A-Za-z0-9_-]"
            )
        m = AUTH_RE.match(auth)
        if not m or m.group("kid") != kid:
            raise VerificationError(
                "invalid_authorization",
                401,
                "Authorization must be 'HMAC-SHA256 Credential=<kid>, Signature=<hex64>'",
            )
        signature = m.group("sig")

        secret = self.registry.get(kid)
        if secret is None:
            raise VerificationError("unknown_key", 401, "unknown key id")

        timestamp = int(ts_raw)
        now = int(self._clock())
        # Boundaries included: a request exactly at +/-window is accepted.
        if abs(now - timestamp) > self.window:
            raise VerificationError(
                "stale_timestamp",
                401,
                f"timestamp outside +/-{self.window}s replay window",
            )

        canonical = canonical_request(
            method,
            target,
            body or b"",
            key_id=kid,
            timestamp=timestamp,
            nonce=nonce,
        )
        if not verify_signature(secret, canonical, signature):
            raise VerificationError("bad_signature", 401, "signature mismatch")

        # Atomic claim: UNIQUE(kid, nonce) makes concurrent duplicates fail.
        if not self.nonces.register(kid, nonce, timestamp, now=now):
            raise VerificationError(
                "replay_detected", 403, "nonce has already been used within the window"
            )

        return AuthenticatedRequest(kid=kid, nonce=nonce, timestamp=timestamp)
