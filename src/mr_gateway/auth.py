"""Authorization tokens for robot command submission.

A token is an HMAC-SHA256-signed, URL-safe compact string carrying:

    kid     key id (v1) — allows future key rotation
    rid     robot id the token is scoped to
    sub     test identity (tester id) the token was issued for
    exp     absolute expiry, unix seconds
    epoch  authorization generation of the robot mapping; every registration
            change for that robot bumps its epoch, immediately invalidating
            tokens minted against the old mapping

Wire form::

    mr1.<b64url(payload)>.<b64url(hmac_sha256(payload))>

The signature covers the payload bytes only, using the configured symmetric
secret. All HMAC and base64 operations are real cryptographic operations from
the Python standard library; nothing here is mocked.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

KID = "mr1"


class TokenError(Exception):
    """Base class for token verification failures."""


class TokenMalformed(TokenError):
    pass


class TokenSignatureInvalid(TokenError):
    pass


class TokenExpired(TokenError):
    pass


class TokenStale(TokenError):
    """Token was signed for a previous mapping epoch (mapping changed)."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(txt: str) -> bytes:
    pad = "=" * (-len(txt) % 4)
    return base64.urlsafe_b64decode(txt + pad)


def _sign(payload_b64: str, secret: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"),
                   hashlib.sha256)
    return _b64e(mac.digest())


def issue_token(
    secret: str,
    robot_id: str,
    tester_id: str,
    epoch: int,
    ttl_seconds: int,
    *,
    now: float | None = None,
) -> str:
    """Mint a fresh token for one (robot, tester) pair at a given epoch."""
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    payload = {
        "kid": KID,
        "rid": robot_id,
        "sub": tester_id,
        "epoch": int(epoch),
        "exp": int((time.time() if now is None else now) + ttl_seconds),
    }
    payload_b64 = _b64e(json.dumps(payload, separators=(",", ":"),
                                   sort_keys=True).encode("utf-8"))
    return f"{KID}.{payload_b64}.{_sign(payload_b64, secret)}"


@dataclass(frozen=True)
class Claims:
    robot_id: str
    tester_id: str
    epoch: int
    expires_at: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "rid": self.robot_id,
            "sub": self.tester_id,
            "epoch": self.epoch,
            "exp": self.expires_at,
        }


def verify_token(
    token: str,
    secret: str,
    expected_robot_id: str,
    current_epoch: int,
    *,
    now: float | None = None,
) -> Claims:
    """Verify signature, expiry, robot binding and mapping epoch.

    Raises a :class:`TokenError` subclass on failure. Distinct subclasses let
    the API return an accurate rejection reason.
    """
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 3 or parts[0] != KID:
        raise TokenMalformed("token must have form mr1.<payload>.<sig>")
    _, payload_b64, sig = parts

    expected_sig = _sign(payload_b64, secret)
    # Constant-time comparison — signature verification must not leak timing.
    if not hmac.compare_digest(sig, expected_sig):
        raise TokenSignatureInvalid("signature mismatch")

    try:
        payload = json.loads(_b64d(payload_b64))
    except Exception as exc:  # noqa: BLE001 - any decode failure => malformed
        raise TokenMalformed("payload undecodable") from exc

    for key in ("rid", "sub", "epoch", "exp"):
        if key not in payload:
            raise TokenMalformed(f"payload missing {key!r}")

    current = time.time() if now is None else now
    if int(payload["exp"]) <= int(current):
        raise TokenExpired("token has expired")
    if payload["rid"] != expected_robot_id:
        # A token minted for robot B presented on robot A's endpoint.
        raise TokenSignatureInvalid("token robot binding does not match")
    if int(payload["epoch"]) != int(current_epoch):
        raise TokenStale(
            f"token epoch {payload['epoch']} != mapping epoch {current_epoch}"
        )

    return Claims(
        robot_id=payload["rid"],
        tester_id=payload["sub"],
        epoch=int(payload["epoch"]),
        expires_at=int(payload["exp"]),
    )
