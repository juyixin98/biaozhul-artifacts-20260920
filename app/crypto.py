"""Real cryptographic operations.

* Content identifiers and integrity digests: SHA-256.
* Snapshot / index authenticity: Ed25519 signatures over canonical JSON.

Nothing here is simulated: signing and verification use the ``cryptography``
library's Ed25519 implementation. The private signing key is generated on first
startup, stored on disk with ``0600`` permissions and reused across restarts so
that previously published signatures stay verifiable.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# B64 (url-safe, no padding) — stable, filesystem-safe representation.
import base64


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON encoding (sorted keys, compact separators, no spaces).

    This is the exact byte string that gets hashed or signed, so signatures are
    independent of dict insertion order.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | os.PathLike[str], chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def digest_json(obj: Any) -> str:
    return sha256_bytes(canonical_json(obj))


class Signer:
    """Loads (or creates) the Ed25519 signing key under ``key_path``."""

    def __init__(self, key_path: str | os.PathLike[str]):
        self.key_path = Path(key_path)
        self._key: Ed25519PrivateKey | None = None

    def load_or_create(self) -> None:
        if self.key_path.exists():
            self._key = serialization.load_pem_private_key(
                self.key_path.read_bytes(), password=None
            )
            if not isinstance(self._key, Ed25519PrivateKey):
                raise ValueError("stored key is not an Ed25519 private key")
        else:
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            self._key = Ed25519PrivateKey.generate()
            tmp = self.key_path.with_suffix(".tmp")
            tmp.write_bytes(
                self._key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.key_path)
            os.chmod(self.key_path, 0o600)

    @property
    def private_key(self) -> Ed25519PrivateKey:
        if self._key is None:
            raise RuntimeError("Signer.load_or_create() must be called first")
        return self._key

    def public_pem(self) -> str:
        return self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")

    def sign_json(self, obj: Any) -> str:
        """Sign canonical JSON, return urlsafe-base64 signature."""
        sig = self.private_key.sign(canonical_json(obj))
        return b64e(sig)

    def verify_json_signature(self, obj: Any, signature_b64: str) -> bool:
        return verify_json(obj, signature_b64, self.public_pem())


def verify_json(obj: Any, signature_b64: str, public_pem: str) -> bool:
    """Verify a detached Ed25519 signature over canonical JSON."""
    try:
        pub = serialization.load_pem_public_key(public_pem.encode("ascii"))
        if not isinstance(pub, Ed25519PublicKey):
            return False
        pub.verify(b64d(signature_b64), canonical_json(obj))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
