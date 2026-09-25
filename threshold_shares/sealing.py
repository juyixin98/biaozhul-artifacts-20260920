"""Optional authenticated passphrase wrapping of shares.

The Shamir construction in this project is *unauthenticated*: shares have no
signature, so a bad share corrupts the recovered secret and the recovery
operation cannot name the malicious participant. This module is deliberately
separate from that claim -- it shows what an *authenticated* layer looks like
using only primitives from the ``cryptography`` package:

  * KDF:   scrypt (RFC 7914), per-seal random salt
  * cipher: AES-256-GCM, per-seal random 96-bit nonce

GCM authenticates the ciphertext AND the header (salt/scrypt parameters), so
tampering with a sealed token is detected on unseal. Authentication here is
between the share holder and their own passphrase; it does NOT authenticate
the share to the combiner and does not let the combiner identify participants.

Token wire form (v1)::

    "SEAL1$" + base64url(
        b"SEAL1" | salt(16) | n(4 BE) | r(4 BE) | p(4 BE)
               | nonce(12) | ciphertext || gcm_tag(16)
    )
"""

import base64
import binascii
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

_PREFIX = "SEAL1$"
_MAGIC = b"SEAL1"
_SALT_LEN = 16
_NONCE_LEN = 12
_KEY_LEN = 32
# Default scrypt cost. Conservative-but-local; override per call if desired.
DEFAULT_SCRYPT_N = 2**14
DEFAULT_SCRYPT_R = 8
DEFAULT_SCRYPT_P = 1
_BASE64URL_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
_HEADER_LEN = 5 + 16 + 4 + 4 + 4 + 12  # = 45: magic, salt, n, r, p, nonce


class SealingError(ValueError):
    """Sealing or unsealing failed (bad parameters, wrong passphrase, tampering)."""


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    if not text or any(ch not in _BASE64URL_ALPHABET for ch in text):
        raise SealingError("token is not canonical unpadded base64url")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise SealingError(f"invalid base64url encoding: {exc}") from exc


def _derive_key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    if not isinstance(passphrase, str) or not passphrase:
        raise SealingError("passphrase must be a non-empty string")
    kdf = Scrypt(salt=salt, length=_KEY_LEN, n=n, r=r, p=p)
    return kdf.derive(passphrase.encode("utf-8"))


def seal_share(encoded_share, passphrase: str, *, n=DEFAULT_SCRYPT_N, r=DEFAULT_SCRYPT_R, p=DEFAULT_SCRYPT_P) -> str:
    """Seal an encoded share string (or arbitrary bytes/str) under a passphrase."""
    if isinstance(encoded_share, str):
        plaintext = encoded_share.encode("utf-8")
    elif isinstance(encoded_share, (bytes, bytearray)):
        plaintext = bytes(encoded_share)
    else:
        raise SealingError("encoded_share must be str or bytes")
    if not plaintext:
        raise SealingError("refusing to seal empty plaintext")
    if n < 2 or (n & (n - 1)) != 0:
        raise SealingError("scrypt n must be a power of two >= 2")
    if r < 1 or p < 1:
        raise SealingError("scrypt r and p must be >= 1")

    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    key = _derive_key(passphrase, salt, n, r, p)
    # Associated data binds the KDF parameters to the ciphertext.
    aad = _MAGIC + salt + n.to_bytes(4, "big") + r.to_bytes(4, "big") + p.to_bytes(4, "big")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    body = (
        _MAGIC
        + salt
        + n.to_bytes(4, "big")
        + r.to_bytes(4, "big")
        + p.to_bytes(4, "big")
        + nonce
        + ciphertext
    )
    return _PREFIX + _b64url_encode(body)


def unseal_share(token: str, passphrase: str) -> str:
    """Unseal a token; raises SealingError on wrong passphrase or tampering."""
    if not isinstance(token, str) or not token.startswith(_PREFIX):
        raise SealingError("not a SEAL1 token")
    raw = _b64url_decode(token[len(_PREFIX) :])
    if len(raw) < _HEADER_LEN + 16:
        raise SealingError("token too short")
    if raw[:5] != _MAGIC:
        raise SealingError("bad magic bytes")
    offset = 5
    salt = raw[offset : offset + _SALT_LEN]
    offset += _SALT_LEN
    n = int.from_bytes(raw[offset : offset + 4], "big")
    r = int.from_bytes(raw[offset + 4 : offset + 8], "big")
    p = int.from_bytes(raw[offset + 8 : offset + 12], "big")
    offset += 12
    nonce = raw[offset : offset + _NONCE_LEN]
    offset += _NONCE_LEN
    ciphertext = raw[offset:]
    if n < 2 or (n & (n - 1)) != 0 or r < 1 or p < 1:
        raise SealingError("invalid scrypt parameters in token")
    aad = _MAGIC + salt + n.to_bytes(4, "big") + r.to_bytes(4, "big") + p.to_bytes(4, "big")
    key = _derive_key(passphrase, salt, n, r, p)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad)
    except Exception as exc:  # cryptography raises InvalidTag; keep the boundary type.
        raise SealingError("unseal failed: wrong passphrase or the token was modified") from exc
    return plaintext.decode("utf-8")
