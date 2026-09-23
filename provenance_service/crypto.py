"""Cryptographic primitives used by the provenance service.

Everything here is *executed* with real implementations:

* Keccak-256 (the pre-SHA3 variant used by Ethereum) via pycryptodome;
* EIP-55 mixed-case address checksums;
* content identifiers (sha256 multihash + base58btc, keccak256 raw);
* canonical JSON serialization;
* Ed25519 signing of evidence receipts via the ``cryptography`` package.

No hash is ever stubbed, no verification short-circuited.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from Crypto.Hash import keccak as _keccak  # pycryptodome: real Keccak
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def keccak256(data: bytes) -> bytes:
    """Return the 32-byte Keccak-256 digest of ``data``."""
    h = _keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Canonical JSON (RFC 8785 style: sorted keys, no whitespace, UTF-8, list order kept)
# ---------------------------------------------------------------------------

def canonical_json(obj: Any) -> bytes:
    """Deterministic byte serialization of JSON-compatible data.

    Object keys are sorted lexicographically; separators carry no whitespace.
    Lists preserve order. Floats are rejected (they have no safe canonical
    form for evidence purposes).
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def json_digest(obj: Any) -> str:
    """keccak256 hex digest of the canonical serialization of ``obj``."""
    return keccak256(canonical_json(obj)).hex()


# ---------------------------------------------------------------------------
# Base58btc / CID
# ---------------------------------------------------------------------------

_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}


def base58btc_encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = bytearray()
    while n > 0:
        n, r = divmod(n, 58)
        out.append(_ALPHABET[r])
    # Leading zero bytes are encoded as leading '1's.
    for b in data:
        if b == 0:
            out.append(_ALPHABET[0])
        else:
            break
    return bytes(reversed(out)).decode("ascii")


def base58btc_decode(s: str) -> bytes:
    n = 0
    for ch in s.encode("ascii"):
        if ch not in _INDEX:
            raise ValueError("invalid base58btc character")
        n = n * 58 + _INDEX[ch]
    out = bytearray(n.to_bytes((n.bit_length() + 7) // 8, "big")) if n else bytearray()
    for ch in s:
        if ch == "1":
            out = b"\x00" + bytes(out)
        else:
            break
    return bytes(out)


def ipfs_cid_v0(sha256_digest: bytes) -> str:
    """Build a CIDv0 (dag-pb, sha2-256 multihash) string from a 32-byte digest."""
    if len(sha256_digest) != 32:
        raise ValueError("CIDv0 requires a 32-byte sha2-256 digest")
    multihash = bytes([0x12, 0x20]) + sha256_digest  # 0x12=sha2-256, 0x20=length32
    return base58btc_encode(multihash)


# ---------------------------------------------------------------------------
# Ethereum addresses (EIP-55)
# ---------------------------------------------------------------------------

def eip55_checksum(address: str) -> str:
    """Return the EIP-55 mixed-case checksummed form of a 0x hex address."""
    if not is_hex_address(address):
        raise ValueError(f"invalid address: {address!r}")
    addr = address[2:].lower()
    h = keccak256(addr.encode("ascii")).hex()
    out = "0x" + "".join(
        c.upper() if c in "abcdef" and int(h[i], 16) >= 8 else c
        for i, c in enumerate(addr)
    )
    return out


def is_hex_address(s: str) -> bool:
    if not isinstance(s, str) or len(s) != 42 or not s.startswith("0x"):
        return False
    try:
        int(s[2:], 16)
    except ValueError:
        return False
    return True


def verify_eip55(address: str) -> bool:
    """True iff ``address`` is a 20-byte address with a valid EIP-55 checksum."""
    if not is_hex_address(address):
        return False
    return address == eip55_checksum(address)


# ---------------------------------------------------------------------------
# Evidence signing (Ed25519)
# ---------------------------------------------------------------------------

def generate_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def save_private_key(key: Ed25519PrivateKey, path: str) -> None:
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with open(path, "wb") as f:
        f.write(pem)


def load_private_key(path: str) -> Ed25519PrivateKey:
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def public_key_hex(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    pub = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()


def sign_message(key: Ed25519PrivateKey, message: bytes) -> bytes:
    return key.sign(message)


def verify_signature(public_hex: str, message: bytes, signature: bytes) -> bool:
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
        pub.verify(signature, message)
        return True
    except (InvalidSignature, ValueError):
        return False


# ---------------------------------------------------------------------------
# Hash-chain helpers
# ---------------------------------------------------------------------------

def chain_hash(prev_chain: str | None, payload_digest: str) -> str:
    """One link of the append-only evidence chain."""
    prev = bytes.fromhex(prev_chain) if prev_chain else b"\x00" * 32
    return keccak256(prev + bytes.fromhex(payload_digest)).hex()


def digests_of_mapping(m: Mapping[str, str]) -> str:
    return json_digest({k: m[k] for k in sorted(m)})
