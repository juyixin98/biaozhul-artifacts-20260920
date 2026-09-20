"""Password hashing and opaque session tokens.

No third-party crypto dependency: PBKDF2-HMAC-SHA256 from the standard library
is sufficient for this system.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets

_ALGORITHM = "sha256"
_ITERATIONS = 260_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac(_ALGORITHM, password.encode(), salt, _ITERATIONS)
    return f"pbkdf2${_ITERATIONS}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt_hex, hash_hex = stored.split("$")
        derived = hashlib.pbkdf2_hmac(
            _ALGORITHM, password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(derived.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)
