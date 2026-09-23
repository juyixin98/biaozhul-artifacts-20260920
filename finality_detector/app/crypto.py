"""Real cryptographic operations built on Ed25519 (RFC 8032).

Every vote is an actual signature over a canonical message that binds the
chain id, epoch, height and proposed value together, with an explicit
domain-separation prefix. Replaying a signature from a different epoch or
chain therefore cannot validate.
"""

from __future__ import annotations

import base64

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# Prefix stops a signature from ever being valid in another message context.
DOMAIN_SEP = "fdd/vote/v1"


def canonical_message(chain_id: str, epoch: int, height: int, value: str) -> bytes:
    r"""Deterministic bytes that are signed.

    Fields are length-prefixed (``len:value``) so no ambiguous concatenation
    is possible; integers are rendered in fixed decimal form.
    """
    parts = [
        DOMAIN_SEP.encode(),
        _field(chain_id),
        _field(str(epoch)),
        _field(str(height)),
        _field(value),
    ]
    return b"|".join(parts)


def _field(value: str) -> bytes:
    data = value.encode("utf-8")
    return f"{len(data)}:".encode() + data


# --------------------------------------------------------------------- keys


def generate_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def encode_public_key(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def decode_public_key(encoded: str) -> Ed25519PublicKey:
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception as exc:  # malformed base64
        raise ValueError("public key is not valid base64") from exc
    if len(raw) != 32:
        raise ValueError("Ed25519 public key must be 32 bytes")
    return Ed25519PublicKey.from_public_bytes(raw)


def encode_private_key(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(raw).decode("ascii")


def decode_private_key(encoded: str) -> Ed25519PrivateKey:
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError("private key is not valid base64") from exc
    if len(raw) != 32:
        raise ValueError("Ed25519 private key must be 32 bytes")
    return Ed25519PrivateKey.from_private_bytes(raw)


# ----------------------------------------------------------------- signing


def sign_vote(
    private_key: Ed25519PrivateKey,
    chain_id: str,
    epoch: int,
    height: int,
    value: str,
) -> str:
    """Sign a vote and return a base64 signature."""
    message = canonical_message(chain_id, epoch, height, value)
    return base64.b64encode(private_key.sign(message)).decode("ascii")


def verify_vote(
    public_key_b64: str,
    signature_b64: str,
    chain_id: str,
    epoch: int,
    height: int,
    value: str,
) -> bool:
    """Return True iff the signature genuinely verifies for this vote."""
    try:
        public_key = decode_public_key(public_key_b64)
        signature = base64.b64decode(signature_b64, validate=True)
    except ValueError:
        return False
    if len(signature) != 64:  # Ed25519 signatures are exactly 64 bytes
        return False
    message = canonical_message(chain_id, epoch, height, value)
    try:
        public_key.verify(signature, message)
    except InvalidSignature:
        return False
    return True
