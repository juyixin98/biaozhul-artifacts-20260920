"""Ed25519 key handling.

The signing key is held by the log server. The corresponding *public* key is
the trust anchor: it must be obtained out-of-band by verifiers and never
served to them by the log being checked.
"""

from __future__ import annotations

from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

KEY_VERSION = "ed25519-v1"


def generate_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def save_private_key(path: str | Path, key: Ed25519PrivateKey) -> None:
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(pem)
    p.chmod(0o600)


def load_private_key(path: str | Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    assert isinstance(key, Ed25519PrivateKey)
    return key


def public_key_hex(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    pub = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()


def load_public_key_hex(hex_key: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(hex_key))


def export_public_key_pem(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def import_public_key_pem(pem: bytes) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(pem)
    assert isinstance(key, Ed25519PublicKey)
    return key


def sign(key: Ed25519PrivateKey, message: bytes) -> bytes:
    return key.sign(message)


def verify_signature(key: Ed25519PublicKey, signature: bytes, message: bytes) -> bool:
    try:
        key.verify(signature, message)
        return True
    except InvalidSignature:
        return False
