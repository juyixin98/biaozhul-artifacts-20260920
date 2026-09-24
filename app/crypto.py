"""Response integrity signing with real HMAC-SHA256 (Python stdlib only).

The signature covers a canonical JSON serialization of the analyze result with
the signing fields removed, so a client can recompute it byte-for-byte. The
shared secret comes from ``TOPOLOGY_SCHED_HMAC_KEY``; a clearly-labelled
development default is used when unset. This is an application-layer integrity
aid, not Kubernetes authentication.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os

DEV_DEFAULT_KEY = "dev-only-integrity-key-do-not-use-in-production"
HMAC_ALGORITHM = "HMAC-SHA256"


def current_key() -> bytes:
    return os.environ.get("TOPOLOGY_SCHED_HMAC_KEY", DEV_DEFAULT_KEY).encode("utf-8")


def canonical_json(payload: dict) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sign(payload: dict, key: bytes | None = None) -> tuple[str, str]:
    """Return (hex digest, algorithm) for a payload dict."""
    digest = hmac.new(key or current_key(), canonical_json(payload), hashlib.sha256)
    return digest.hexdigest(), HMAC_ALGORITHM


def verify(payload: dict, signature_hex: str, key: bytes | None = None) -> bool:
    expected, _ = sign(payload, key)
    return hmac.compare_digest(expected, signature_hex or "")


def signed_result(result: dict) -> dict:
    """Attach signature fields covering everything except the fields themselves."""
    covered = {k: v for k, v in result.items() if k not in ("signature", "hmacAlgorithm")}
    digest, algorithm = sign(covered)
    return {**covered, "signature": digest, "hmacAlgorithm": algorithm}
