"""Real cryptographic operations for published calibration models.

Each published model is serialized as *canonical JSON* (sorted keys, compact
separators, UTF-8, ``allow_nan=False`` so non-finite numbers can never leak
into a signed payload) and authenticated with HMAC-SHA256.  Signatures are
verified with :func:`hmac.compare_digest` (constant-time comparison).

The signing key is a 256-bit value generated with :func:`secrets.token_bytes`
on first use and stored in a file readable/writable only by the service user
(mode 0600).  No network, no placeholder primitives.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any

KEY_ENV = "CLOCKDRIFT_KEY_FILE"
DEFAULT_KEY_PATH = Path("./data/signing.key")


def canonical_json(payload: Any) -> bytes:
    """Deterministic byte serialization used as the signed payload."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def load_or_create_key(key_path: Path | str | None = None) -> bytes:
    """Load the HMAC key, generating a new 256-bit key on first use."""
    path = Path(key_path or os.environ.get(KEY_ENV, DEFAULT_KEY_PATH))
    if path.exists():
        key = path.read_bytes()
        if len(key) < 16:
            raise ValueError(f"signing key at {path} is too short to be safe")
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    # Write with tight permissions from the start.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def sign(payload: Any, key: bytes) -> str:
    """Return hex HMAC-SHA256 over the canonical payload."""
    return hmac.new(key, canonical_json(payload), hashlib.sha256).hexdigest()


def verify(payload: Any, signature_hex: str, key: bytes) -> bool:
    """Constant-time signature verification."""
    try:
        expected = sign(payload, key)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, signature_hex or "")
