"""Real cryptographic signing of published calibration versions.

Each published version is serialised with a deterministic canonical JSON
encoding and signed with Ed25519 (RFC 8032).  The signing key is generated on
first start with ``os.urandom`` and stored under the data directory; the
matching public key is exposed so consumers can verify versions offline.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64d(txt: str) -> bytes:
    pad = "=" * (-len(txt) % 4)
    return base64.urlsafe_b64decode(txt + pad)


def canonical_json(obj) -> bytes:
    """Deterministic encoding: sorted keys, no whitespace, compact floats."""

    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


class Signer:
    def __init__(self, key_dir: Path):
        key_dir.mkdir(parents=True, exist_ok=True)
        priv_path = key_dir / "ed25519_private.pem"
        if priv_path.exists():
            self._key = serialization.load_pem_private_key(
                priv_path.read_bytes(), password=None
            )
            assert isinstance(self._key, Ed25519PrivateKey)
        else:
            self._key = Ed25519PrivateKey.generate()
            # os.chmod first so the PEM is never briefly world-readable
            priv_path.touch(mode=0o600)
            priv_path.write_bytes(
                self._key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )
            os.chmod(priv_path, 0o600)
        self._pub: Ed25519PublicKey = self._key.public_key()

    def public_key_b64(self) -> str:
        raw = self._pub.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return b64e(raw)

    def sign_payload(self, payload: dict) -> dict:
        """Return an envelope containing the payload and its signature."""

        body = canonical_json(payload)
        sig = self._key.sign(body)
        return {
            "alg": "Ed25519",
            "public_key": self.public_key_b64(),
            "payload": payload,
            "signature": b64e(sig),
        }


def verify_envelope(envelope: dict) -> tuple[bool, str]:
    """Verify a signed envelope produced by :meth:`Signer.sign_payload`."""

    try:
        if envelope.get("alg") != "Ed25519":
            return False, "unsupported alg"
        pub = Ed25519PublicKey.from_public_bytes(b64d(envelope["public_key"]))
        body = canonical_json(envelope["payload"])
        pub.verify(b64d(envelope["signature"]), body)
        return True, "valid"
    except (KeyError, ValueError) as exc:
        return False, f"malformed envelope: {exc}"
    except InvalidSignature:
        return False, "signature does not verify"
