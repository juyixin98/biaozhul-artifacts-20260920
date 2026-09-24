"""Ed25519 signing of analysis reports (uses the `cryptography` package).

A server key is generated on first boot and persisted under a data
directory. Clients can fetch the public key via GET /public-key and
verify the ``signature`` field of an analysis response:

    canonical = json.dumps(response["report"],
                           sort_keys=True, separators=(",", ":")).encode()
    verify(public_key_pem, base64.b64decode(response["signature"]), canonical)
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

DEFAULT_KEY_DIR = Path(os.environ.get("TAINT_KEY_DIR", ".data"))
KEY_PATH = DEFAULT_KEY_DIR / "ed25519_key.pem"


def canonical_json(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


class Signer:
    def __init__(self, key_path: Path = KEY_PATH):
        self.key_path = key_path
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if key_path.exists():
            self.private_key = serialization.load_pem_private_key(
                key_path.read_bytes(), password=None)
            if not isinstance(self.private_key, Ed25519PrivateKey):
                raise ValueError("stored key is not an Ed25519 key")
        else:
            self.private_key = Ed25519PrivateKey.generate()
            key_path.write_bytes(self.private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption()))
            key_path.chmod(0o600)

    def public_key_pem(self) -> bytes:
        return self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo)

    def sign(self, obj: object) -> str:
        sig = self.private_key.sign(canonical_json(obj))
        return base64.b64encode(sig).decode("ascii")


def verify(public_key_pem: bytes, signature_b64: str, obj: object) -> bool:
    try:
        key = serialization.load_pem_public_key(public_key_pem)
        if not isinstance(key, Ed25519PublicKey):
            return False
        key.verify(base64.b64decode(signature_b64), canonical_json(obj))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
