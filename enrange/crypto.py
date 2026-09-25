"""Cryptographic primitives for enrange.

Design choices (no home-grown primitives):

* AEAD: AES-256-GCM from the audited ``cryptography`` library.
* Key derivation: HKDF-SHA256 (RFC 5869) from the master key. Each object gets
  an independent per-object key, so a single block/key failure never crosses
  objects and the master key never touches an AEAD operation directly.
* Nonces: GCM nonces must never repeat under one key. We use deterministic
  counter nonces (uniqueness is structural, not left to RNG luck):
    - block i : b"\\x00blk" + u64be(i)
    - header  : b"\\x00hdr!" + 7 zero bytes
  The two spaces share an explicit disjoint prefix.
* Associated data binds identity, position and length, so ciphertext blocks
  cannot be moved, swapped, truncated or reused across objects without the AEAD
  verification failing.

AAD wire formats (length-prefixed fields are big-endian u32 lengths):

  header AAD: b"enrange-v1-hdr\\0" || u32(len(id)) || id || u32(block_size)
              || u64(plaintext_len) || u64(encapsulated_bytes)

  block  AAD: b"enrange-v1-blk\\0" || u32(len(id)) || id || u32(block_size)
              || u64(block_index) || u64(plaintext_offset)
              || u32(plaintext_len) || u64(object_plaintext_len)
              || u32(total_blocks)
"""

from __future__ import annotations

import os
import struct

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MASTER_KEY_BYTES = 32  # AES-256
OBJECT_KEY_BYTES = 32  # AES-256
NONCE_BYTES = 12  # GCM standard nonce size
TAG_BYTES = 16  # GCM tag size

_VERSION = b"enrange-v1"
_HEADER_INFO = b"enrange-v1-master-key-header"
_OBJECT_KEY_INFO_PREFIX = b"enrange-v1-object-key:"
_HEADER_AAD_PREFIX = _VERSION + b"-hdr\x00"
_BLOCK_AAD_PREFIX = _VERSION + b"-blk\x00"
_BLOCK_NONCE_PREFIX = b"\x00blk"  # 4 bytes, followed by u64 block index
_HEADER_NONCE = b"\x00hdr!" + b"\x00" * 7  # 12 bytes; disjoint from block space


def generate_master_key() -> bytes:
    """Generate a fresh random 256-bit master key (local test keys only)."""
    return os.urandom(MASTER_KEY_BYTES)


def derive_object_key(master_key: bytes, object_id: bytes) -> bytes:
    """Derive an independent AES-256 key for one object.

    The object id is used as HKDF *salt* and a fixed label as *info*: distinct
    random object ids then produce independent keys, and every caller is
    forced to bind the object identity into key derivation.
    """
    if len(master_key) != MASTER_KEY_BYTES:
        raise ValueError("master key must be exactly 32 bytes")
    if not object_id:
        raise ValueError("object_id must not be empty")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=OBJECT_KEY_BYTES,
        salt=object_id,
        info=_OBJECT_KEY_INFO_PREFIX,
    ).derive(master_key)


def _aead(object_key: bytes) -> AESGCM:
    if len(object_key) != OBJECT_KEY_BYTES:
        raise ValueError("object key must be exactly 32 bytes")
    return AESGCM(object_key)


def block_nonce(block_index: int) -> bytes:
    """Deterministic GCM nonce for block ``block_index`` (0-based)."""
    if not 0 <= block_index < 2**64:
        raise ValueError("block_index out of u64 range")
    return _BLOCK_NONCE_PREFIX + struct.pack(">Q", block_index)


def header_aad(
    object_id: bytes,
    block_size: int,
    plaintext_len: int,
    encapsulated_bytes: int,
) -> bytes:
    return (
        _HEADER_AAD_PREFIX
        + struct.pack(">I", len(object_id))
        + object_id
        + struct.pack(">I", block_size)
        + struct.pack(">Q", plaintext_len)
        + struct.pack(">Q", encapsulated_bytes)
    )


def block_aad(
    object_id: bytes,
    block_size: int,
    block_index: int,
    plaintext_offset: int,
    plaintext_len: int,
    object_plaintext_len: int,
    total_blocks: int,
) -> bytes:
    return (
        _BLOCK_AAD_PREFIX
        + struct.pack(">I", len(object_id))
        + object_id
        + struct.pack(">I", block_size)
        + struct.pack(">Q", block_index)
        + struct.pack(">Q", plaintext_offset)
        + struct.pack(">I", plaintext_len)
        + struct.pack(">Q", object_plaintext_len)
        + struct.pack(">I", total_blocks)
    )


def seal_header(object_key: bytes, plaintext_header: bytes, aad: bytes) -> bytes:
    """Encrypt + authenticate the (empty) header payload.

    The header tag covers the AAD that carries object length/block metadata;
    the encrypted payload itself is empty (kept as a byte field for format
    extensibility).
    """
    return _aead(object_key).encrypt(_HEADER_NONCE, plaintext_header, aad)


def open_header(object_key: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    """Verify and decrypt header bytes. Raises InvalidTag on tampering."""
    return _aead(object_key).decrypt(_HEADER_NONCE, ciphertext, aad)


def seal_block(object_key: bytes, block_index: int, plaintext: bytes, aad: bytes) -> bytes:
    return _aead(object_key).encrypt(block_nonce(block_index), plaintext, aad)


def open_block(object_key: bytes, block_index: int, ciphertext: bytes, aad: bytes) -> bytes:
    """Verify and decrypt one block. Raises InvalidTag on tampering."""
    return _aead(object_key).decrypt(block_nonce(block_index), ciphertext, aad)


# Kept constant for completeness; the current header payload is always empty.
HEADER_PAYLOAD = b""
