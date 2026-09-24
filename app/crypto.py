"""Real cryptographic operations: canonical JSON fingerprints and Ed25519.

No placeholder hashes or fake signatures — every routine calls the
``cryptography`` library (Ed25519 over RFC 8032) and ``hashlib`` (SHA-256).

Canonical form: ``json.dumps`` with sort_keys=True, compact separators,
ensure_ascii=False, and the signature fields stripped before fingerprinting,
so a signature never signs itself.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
    load_pem_public_key,
)

SIGNATURE_DROP_KEYS = {"signature"}


def canonical_json(payload: dict, *, drop: set[str] | None = None) -> bytes:
    drop = SIGNATURE_DROP_KEYS if drop is None else set(drop)
    clean = {k: v for k, v in payload.items() if k not in drop}
    return json.dumps(
        clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def fingerprint(payload: dict) -> str:
    """SHA-256 hex fingerprint of the canonical payload."""
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


def public_key_hex(pub: Ed25519PublicKey) -> str:
    return pub.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def private_key_pem(priv: Ed25519PrivateKey) -> bytes:
    return priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


def public_key_pem(pub: Ed25519PublicKey) -> bytes:
    return pub.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)


def save_dev_keypair(priv_path: str | Path, pub_path: str | Path) -> tuple[Path, Path]:
    """Generate a FRESH Ed25519 keypair and write it (development convenience)."""
    priv, pub = generate_keypair()
    priv_path, pub_path = Path(priv_path), Path(pub_path)
    priv_path.write_bytes(private_key_pem(priv))
    pub_path.write_bytes(public_key_pem(pub))
    priv_path.chmod(0o600)
    return priv_path, pub_path


def load_private_key(path: str | Path) -> Ed25519PrivateKey:
    key = load_pem_private_key(Path(path).read_bytes(), password=None)
    assert isinstance(key, Ed25519PrivateKey)
    return key


def load_public_key(path: str | Path) -> Ed25519PublicKey:
    key = load_pem_public_key(Path(path).read_bytes())
    assert isinstance(key, Ed25519PublicKey)
    return key


def public_key_from_hex(h: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(h))


def sign(priv: Ed25519PrivateKey, payload: dict) -> str:
    """Return hex Ed25519 signature over the canonical payload."""
    return priv.sign(canonical_json(payload)).hex()


def sign_bytes(priv: Ed25519PrivateKey, blob: bytes) -> str:
    return priv.sign(blob).hex()


def verify(pub: Ed25519PublicKey, payload: dict, signature_hex: str) -> bool:
    try:
        pub.verify(bytes.fromhex(signature_hex), canonical_json(payload))
        return True
    except (InvalidSignature, ValueError):
        return False


def verify_bytes(pub: Ed25519PublicKey, blob: bytes, signature_hex: str) -> bool:
    try:
        pub.verify(bytes.fromhex(signature_hex), blob)
        return True
    except (InvalidSignature, ValueError):
        return False


def envelope(payload: dict, priv: Ed25519PrivateKey) -> dict:
    """Attach fingerprint, timestamp, key id and Ed25519 signature.

    Every non-signature field (including ``public_key``) is added BEFORE
    signing, so the canonical form a verifier rebuilds (dropping only
    ``signature``) matches the signed bytes exactly.
    """
    body = dict(payload)
    body["fingerprint_sha256"] = fingerprint(payload)
    body["signed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    body["signature_algorithm"] = "Ed25519"
    body["public_key"] = public_key_hex(priv.public_key())
    body["signature"] = sign(priv, body)
    return body
