"""Integrity parsing and *real* cryptographic verification.

npm lockfiles carry Subresource Integrity strings such as
``sha512-<base64-digest>``. We parse those strings strictly, and when the
uploaded bundle includes the referenced tarball (``vendor/`` cache or
nested package tarballs) we recompute the digest from bytes and compare it
ourselves — no network, no shell, no install scripts.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Optional

_INTEGRITY_RE = re.compile(r"^(sha(?:256|384|512))-([A-Za-z0-9+/]+={0,2})$")
_ALG_LEN = {"sha256": 32, "sha384": 48, "sha512": 64}


class IntegrityError(ValueError):
    pass


@dataclass(frozen=True)
class Integrity:
    algorithm: str
    digest: bytes  # raw bytes

    @property
    def sri(self) -> str:
        return f"{self.algorithm}-{base64.b64encode(self.digest).decode()}"


def parse_integrity(text: Optional[str]) -> Integrity:
    if text is None or not str(text).strip():
        raise IntegrityError("missing integrity field")
    candidate = str(text).strip()
    # multiple-hash SRI ("sha512-... sha512-...") is legal in browsers but
    # npm lockfiles emit exactly one; refuse ambiguity rather than guess.
    parts = candidate.split()
    if len(parts) != 1:
        raise IntegrityError(f"expected exactly one integrity hash, got {len(parts)}")
    m = _INTEGRITY_RE.match(parts[0])
    if not m:
        raise IntegrityError(f"malformed integrity value: {parts[0][:32]!r}")
    alg, b64 = m.groups()
    try:
        digest = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise IntegrityError(f"integrity digest is not valid base64: {exc}") from exc
    if len(digest) != _ALG_LEN[alg]:
        raise IntegrityError(
            f"{alg} digest must be {_ALG_LEN[alg]} bytes, got {len(digest)}"
        )
    return Integrity(algorithm=alg, digest=digest)


def compute_digest(data: bytes, algorithm: str = "sha512") -> bytes:
    if algorithm not in _ALG_LEN:
        raise IntegrityError(f"unsupported hash algorithm: {algorithm}")
    return hashlib.new(algorithm, data).digest()


def verify(data: bytes, integrity: Integrity) -> bool:
    """Constant-time comparison of recomputed digest vs declared digest."""
    actual = compute_digest(data, integrity.algorithm)
    return hmac.compare_digest(actual, integrity.digest)
