"""Ed25519 signing of Signed Tree Heads, with explicit domain separation.

Keys are generated locally for testing and stored as raw 32-byte seeds.
No production accounts, no hosted KMS: this module exists to demonstrate
that a client can authenticate a tree head before trusting it.

The signed message is not the bare root hash; it is prefixed with a
fixed context string so a signature cannot be replayed over a different
protocol message:

    "TL-STH-v1" || u64be(tree_size) || root_hash(32) || u64be(timestamp_us)
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import Tuple

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PrivateFormat,
    PublicFormat,
    NoEncryption,
)

STH_CONTEXT = b"TL-STH-v1"


def encode_sth(tree_size: int, root_hash: bytes, timestamp_us: int) -> bytes:
    """Canonical byte string covered by the STH signature."""
    if len(root_hash) != 32:
        raise ValueError("root_hash must be 32 bytes")
    return (
        STH_CONTEXT
        + struct.pack(">Q", tree_size)
        + root_hash
        + struct.pack(">Q", timestamp_us)
    )


@dataclass(frozen=True)
class SignedTreeHead:
    tree_size: int
    root_hash: bytes
    timestamp_us: int
    signature: bytes

    def to_dict(self) -> dict:
        import base64

        return {
            "tree_size": self.tree_size,
            "root_hash": base64.b64encode(self.root_hash).decode("ascii"),
            "timestamp_us": self.timestamp_us,
            "signature": base64.b64encode(self.signature).decode("ascii"),
            "sig_algorithm": "Ed25519",
            "signed_bytes_context": STH_CONTEXT.decode("ascii"),
        }


class KeyManager:
    """Load or generate a local Ed25519 keypair (test use only)."""

    def __init__(self, key_path: str):
        self.key_path = key_path
        if os.path.exists(key_path):
            with open(key_path, "rb") as fh:
                seed = fh.read()
            if len(seed) != 32:
                raise ValueError(f"bad key seed in {key_path} (need 32 bytes)")
            self._private = Ed25519PrivateKey.from_private_bytes(seed)
        else:
            os.makedirs(os.path.dirname(os.path.abspath(key_path)), exist_ok=True)
            self._private = Ed25519PrivateKey.generate()
            raw = self._private.private_bytes_raw()
            # Write then restrict permissions; owner-only read/write.
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, raw)
                os.fsync(fd)
            finally:
                os.close(fd)
        self._public = self._private.public_key()

    def public_key_raw(self) -> bytes:
        return self._public.public_bytes_raw()

    def sign_sth(self, tree_size: int, root_hash: bytes, timestamp_us: int) -> SignedTreeHead:
        msg = encode_sth(tree_size, root_hash, timestamp_us)
        sig = self._private.sign(msg)
        return SignedTreeHead(tree_size, root_hash, timestamp_us, sig)

    def export_private_pem(self) -> bytes:
        return self._private.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        )

    def export_public_pem(self) -> bytes:
        return self._public.public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        )


def verify_sth_signature(
    public_key: bytes,
    tree_size: int,
    root_hash: bytes,
    timestamp_us: int,
    signature: bytes,
) -> Tuple[bool, str]:
    """Verify an STH signature.  Returns (ok, reason)."""
    try:
        if len(public_key) != 32:
            return False, "public key must be 32 raw Ed25519 bytes"
        key = Ed25519PublicKey.from_public_bytes(public_key)
        key.verify(signature, encode_sth(tree_size, root_hash, timestamp_us))
        return True, "ok"
    except Exception as exc:  # InvalidSignature, ValueError, ...
        return False, f"signature verification failed: {type(exc).__name__}"
