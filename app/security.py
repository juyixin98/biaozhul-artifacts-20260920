import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import settings


def _master_key() -> bytes:
    if not settings.master_key:
        raise RuntimeError("VAULT_MASTER_KEY is not configured")
    key = base64.b64decode(settings.master_key)
    if len(key) != 32:
        raise RuntimeError("VAULT_MASTER_KEY must decode to 32 bytes")
    return key


def encrypt_secret(plaintext: bytes, aad: bytes) -> str:
    """AES-256-GCM with a random 96-bit nonce; output is base64(nonce || ciphertext)."""
    nonce = os.urandom(12)
    ct = AESGCM(_master_key()).encrypt(nonce, plaintext, aad)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt_secret(token: str, aad: bytes) -> bytes:
    raw = base64.b64decode(token)
    return AESGCM(_master_key()).decrypt(raw[:12], raw[12:], aad)
