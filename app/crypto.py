"""Cryptographic fingerprinting and integrity signatures.

Two real operations (standard library only, no placeholders):

* ``fingerprint`` -- SHA-256 over a canonical JSON serialization of the
  *parsed* database, so formatting-only edits of the DBC (whitespace,
  comments) do not change the signal-definition version, while any semantic
  change does. The raw source hash is also stored for provenance.
* ``sign`` / ``verify`` -- HMAC-SHA256 over a definition's canonical
  fingerprint, keyed by a server-side secret. The secret comes from the
  ``CANDECODE_HMAC_KEY`` environment variable; when unset a 32-byte random
  key is generated on first use and persisted in the SQLite ``meta`` table so
  signatures remain verifiable across restarts of the same deployment.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets

from .dbc import Database

HMAC_KEY_META_KEY = "hmac_secret_key"
HMAC_ENV_VAR = "CANDECODE_HMAC_KEY"


def canonical_json(database: Database) -> str:
    return json.dumps(
        database.to_canonical(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(database: Database) -> str:
    """Return the signal-definition version id (64 hex chars, SHA-256)."""

    return hashlib.sha256(canonical_json(database).encode("utf-8")).hexdigest()


def sign(version_id: str, key: bytes) -> str:
    return hmac.new(key, version_id.encode("utf-8"), hashlib.sha256).hexdigest()


def verify(version_id: str, signature: str, key: bytes) -> bool:
    return hmac.compare_digest(sign(version_id, key), signature)


def resolve_key(persisted: str | None) -> bytes:
    """Pick the HMAC key: env var first, persisted key second, else new one."""

    env_key = os.environ.get(HMAC_ENV_VAR)
    if env_key:
        if len(env_key.encode("utf-8")) < 16:
            raise RuntimeError(
                f"{HMAC_ENV_VAR} must be at least 16 bytes long"
            )
        return env_key.encode("utf-8")
    if persisted:
        return persisted.encode("ascii")
    return secrets.token_bytes(32).hex().encode("ascii")
