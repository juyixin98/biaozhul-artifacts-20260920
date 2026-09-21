"""Custodial key protection: AES-256-GCM under a local master key.

Boundary rules enforced here and by the API layer:
  * the plaintext private key exists only inside short-lived local variables;
  * it is never logged, placed in an exception message, or returned;
  * only the GCM blob (12-byte nonce || ciphertext+tag) is persisted.
"""
from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings

_NONCE_BYTES = 12


def _master_key() -> bytes:
    return settings.master_key()


def encrypt_private_key(private_key_hex: str) -> bytes:
    """Encrypt a 0x-prefixed (or raw 32-byte) EVM private key."""
    material = _normalize_private_key(private_key_hex)  # validates; local only
    nonce = os.urandom(_NONCE_BYTES)
    blob = nonce + AESGCM(_master_key()).encrypt(nonce, material, None)
    return blob


def decrypt_private_key(blob: bytes) -> bytes:
    """Return the raw 32-byte private key. Callers must not persist/log it."""
    if not blob or len(blob) <= _NONCE_BYTES:
        raise ValueError("malformed ciphertext")
    nonce, ciphertext = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    try:
        return AESGCM(_master_key()).decrypt(nonce, ciphertext, None)
    except InvalidTag:
        # Do not echo key material or ciphertext.
        raise ValueError("custodial key decryption failed") from None


def _normalize_private_key(private_key_hex: str) -> bytes:
    """Validate via the mature eth-keys library; never home-rolled crypto."""
    from eth_keys import keys as eth_keys

    if not isinstance(private_key_hex, str):
        raise ValueError("private key must be a hex string")
    raw_hex = private_key_hex[2:] if private_key_hex.startswith(("0x", "0X")) else private_key_hex
    try:
        raw = bytes.fromhex(raw_hex)
    except ValueError:
        raise ValueError("private key must be hexadecimal") from None
    if len(raw) != 32:
        raise ValueError("private key must be exactly 32 bytes")
    # Constructing PrivateKey validates the scalar range (0 < k < secp256k1 n).
    pk = eth_keys.PrivateKey(raw)
    # Touch the address derivation once to be sure the key is usable, but the
    # address itself is returned through the normal flow, not logged.
    _ = pk.public_key.to_checksum_address()
    return raw
