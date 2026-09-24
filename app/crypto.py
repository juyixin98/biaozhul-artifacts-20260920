"""Envelope encryption core.

Binary object layout (all integers big-endian)::

    magic        4 bytes   b"ENV1"  (format version)
    kek_version  4 bytes   version of the master key that wrapped the DEK
    wrap_nonce   12 bytes  nonce used to wrap the DEK
    wrapped_dek  48 bytes  32-byte DEK encrypted with AES-256-GCM (+16B tag)
    data_nonce   12 bytes  nonce used for the data ciphertext
    ciphertext   rest      plaintext encrypted with AES-256-GCM (+16B tag)

Authenticated data:
  * DEK wrap:   magic || kek_version   -> the header's key version is
    authenticated; tampering with it makes unwrapping fail.
  * data:       magic || data_nonce    -> the format version is authenticated.
    The data AAD deliberately excludes kek_version / wrapped_dek so that a
    master-key rotation can re-wrap the DEK without re-encrypting the data.
"""

from __future__ import annotations

import os
import struct

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"ENV1"
DEK_LEN = 32
NONCE_LEN = 12
TAG_LEN = 16
WRAPPED_DEK_LEN = DEK_LEN + TAG_LEN
HEADER_LEN = 4 + 4 + NONCE_LEN + WRAPPED_DEK_LEN + NONCE_LEN  # 80


class EnvelopeError(Exception):
    """Raised for any parsing or authentication failure.

    Never carries plaintext or key material.
    """


def _wrap_aad(kek_version: int) -> bytes:
    return MAGIC + struct.pack(">I", kek_version)


def _data_aad(data_nonce: bytes) -> bytes:
    return MAGIC + data_nonce


def generate_kek() -> bytes:
    """Generate a new 256-bit master key (key-encryption key)."""
    return AESGCM.generate_key(bit_length=256)


def encrypt(kek: bytes, kek_version: int, plaintext: bytes) -> bytes:
    """Encrypt ``plaintext`` under a fresh per-object DEK wrapped by ``kek``."""
    dek = os.urandom(DEK_LEN)
    wrap_nonce = os.urandom(NONCE_LEN)
    data_nonce = os.urandom(NONCE_LEN)

    wrapped_dek = AESGCM(kek).encrypt(wrap_nonce, dek, _wrap_aad(kek_version))
    ciphertext = AESGCM(dek).encrypt(data_nonce, plaintext, _data_aad(data_nonce))

    header = (
        MAGIC
        + struct.pack(">I", kek_version)
        + wrap_nonce
        + wrapped_dek
        + data_nonce
    )
    return header + ciphertext


def parse_header(blob: bytes) -> tuple[int, bytes, bytes, bytes]:
    """Split off and validate the header. Raises EnvelopeError on any defect."""
    if len(blob) < HEADER_LEN:
        raise EnvelopeError("object too short: truncated header")
    if blob[:4] != MAGIC:
        raise EnvelopeError("bad magic / unsupported format version")
    kek_version = struct.unpack(">I", blob[4:8])[0]
    wrap_nonce = blob[8:20]
    wrapped_dek = blob[20:68]
    data_nonce = blob[68:80]
    if len(blob) == HEADER_LEN:
        raise EnvelopeError("object too short: missing ciphertext")
    return kek_version, wrap_nonce, wrapped_dek, data_nonce


def _unwrap_dek(
    kek: bytes, kek_version: int, wrap_nonce: bytes, wrapped_dek: bytes
) -> bytes:
    try:
        dek = AESGCM(kek).decrypt(
            wrap_nonce, wrapped_dek, _wrap_aad(kek_version)
        )
    except Exception:
        raise EnvelopeError("DEK unwrap failed: wrong key or tampered header")
    if len(dek) != DEK_LEN:
        raise EnvelopeError("DEK unwrap failed: bad DEK length")
    return dek


def decrypt(get_kek, blob: bytes) -> bytes:
    """Decrypt an object. ``get_kek(version)`` must return the KEK bytes or
    raise KeyError. Authentication is verified before any plaintext is
    returned; on failure only EnvelopeError is raised, never partial output."""
    kek_version, wrap_nonce, wrapped_dek, data_nonce = parse_header(blob)
    try:
        kek = get_kek(kek_version)
    except KeyError:
        raise EnvelopeError(f"unknown KEK version {kek_version}")
    dek = _unwrap_dek(kek, kek_version, wrap_nonce, wrapped_dek)
    try:
        return AESGCM(dek).decrypt(
            data_nonce, blob[HEADER_LEN:], _data_aad(data_nonce)
        )
    except Exception:
        raise EnvelopeError("data decryption failed: tampered or truncated ciphertext")


def rewrap(get_kek, current_kek: bytes, current_version: int, blob: bytes) -> bytes:
    """Re-wrap the DEK of ``blob`` under ``current_kek``. The data ciphertext
    and its nonce are left untouched, so rotation never re-encrypts data."""
    kek_version, wrap_nonce, wrapped_dek, data_nonce = parse_header(blob)
    if kek_version == current_version:
        return blob  # already wrapped with the current master key
    try:
        old_kek = get_kek(kek_version)
    except KeyError:
        raise EnvelopeError(f"unknown KEK version {kek_version}")
    dek = _unwrap_dek(old_kek, kek_version, wrap_nonce, wrapped_dek)

    new_wrap_nonce = os.urandom(NONCE_LEN)
    new_wrapped_dek = AESGCM(current_kek).encrypt(
        new_wrap_nonce, dek, _wrap_aad(current_version)
    )
    header = (
        MAGIC
        + struct.pack(">I", current_version)
        + new_wrap_nonce
        + new_wrapped_dek
        + data_nonce
    )
    return header + blob[HEADER_LEN:]
