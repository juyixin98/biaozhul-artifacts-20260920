"""Report signing with Ed25519 (cryptography library).

Every analysis report produced by the service is signed so a consumer can
verify it was produced by this server and was not tampered with.  The key is
generated once per process unless a PEM-encoded private key is supplied via
the ``TAINT_SIGNING_KEY`` environment variable (Ed25519, PKCS8 PEM,
optionally encrypted; set ``TAINT_SIGNING_KEY_PASSWORD`` when encrypted).

Why signing is here: it authenticates the *security verdict* -- a downgrade
from "vulnerable" to "safe" in transit is detectable.  Signing is not part of
the taint analysis itself.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


@dataclass(frozen=True)
class KeyPair:
    private: Ed25519PrivateKey
    public: Ed25519PublicKey


def load_or_create_keypair() -> KeyPair:
    pem = os.environ.get("TAINT_SIGNING_KEY")
    if pem:
        password = os.environ.get("TAINT_SIGNING_KEY_PASSWORD")
        private = serialization.load_pem_private_key(
            pem.encode("utf-8"),
            password=password.encode("utf-8") if password else None,
        )
        if not isinstance(private, Ed25519PrivateKey):
            raise ValueError("TAINT_SIGNING_KEY must be an Ed25519 key")
        return KeyPair(private=private, public=private.public_key())
    private = Ed25519PrivateKey.generate()
    return KeyPair(private=private, public=private.public_key())


def canonical_bytes(report: dict) -> bytes:
    """Deterministic JSON serialization used as the signed payload."""
    return json.dumps(
        report, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sign_report(report: dict, pair: KeyPair) -> dict:
    payload = canonical_bytes(report)
    signature = pair.private.sign(payload)
    public_pem = pair.public.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    signed = dict(report)
    signed["signature"] = {
        "algorithm": "Ed25519",
        "canonical_json": payload.decode("utf-8"),
        "signature_b64": _b64(signature),
        "public_key_pem": public_pem,
    }
    return signed


def verify_report(signed_report: dict) -> bool:
    sig = signed_report.get("signature")
    if not sig:
        return False
    report = {k: v for k, v in signed_report.items() if k != "signature"}
    try:
        public_key = serialization.load_pem_public_key(
            sig["public_key_pem"].encode("ascii")
        )
        if not isinstance(public_key, Ed25519PublicKey):
            return False
        public_key.verify(
            _b64decode(sig["signature_b64"]),
            canonical_bytes(report),
        )
        return True
    except (InvalidSignature, KeyError, ValueError, TypeError):
        return False
