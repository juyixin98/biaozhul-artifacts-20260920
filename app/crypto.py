"""Real cryptographic primitives used for artifact signing.

- SHA-256 content hashing for images and canonical payloads.
- HMAC-SHA256 signatures over canonical JSON of calibration versions.

Nothing here is stubbed: hmac/hashlib from the Python standard library.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from pathlib import Path
from typing import Any

from . import config


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def canonical_json(obj: Any) -> bytes:
    """Deterministic serialization: sorted keys, compact separators, no ASCII escaping."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def payload_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def _load_or_create_key() -> bytes:
    import os

    key_file = config.DATA_DIR / "hmac.key"
    if key_file.exists():
        key = key_file.read_bytes().strip()
        if key:
            return key
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32).hex().encode("ascii")
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
        os.fsync(fd)
    finally:
        os.close(fd)
    return key


def get_secret_key() -> bytes:
    if config.SECRET_KEY_ENV:
        return config.SECRET_KEY_ENV.encode("utf-8")
    return _load_or_create_key()


def sign_payload(obj: Any) -> str:
    """Return 'sha256=<hmac hex>' over the canonical JSON of obj."""
    mac = hmac.new(get_secret_key(), canonical_json(obj), hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def verify_payload(obj: Any, signature: str) -> bool:
    expected = sign_payload(obj)
    # constant-time comparison
    return bool(signature) and hmac.compare_digest(expected, signature)
