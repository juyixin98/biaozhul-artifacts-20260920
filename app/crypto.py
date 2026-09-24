"""Cryptographic primitives for checkpoints.

Checkpoints are signed with HMAC-SHA256 over a canonical JSON encoding, so a
tampered or foreign checkpoint is rejected before any session state is
restored. The secret key comes from ``REPLAY_HMAC_KEY`` when set, otherwise a
256-bit random key is generated once and persisted with 0600 permissions in
the state directory.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any

SIGNATURE_ALG = "HMAC-SHA256"
SIGNATURE_FIELD = "signature"
PAYLOAD_FIELD = "checkpoint"


class CheckpointSignatureError(ValueError):
    """The checkpoint signature is missing, malformed or does not verify."""


def canonical_json(payload: Any) -> bytes:
    """Deterministic byte encoding used for signing."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def load_or_create_secret(path: Path) -> bytes:
    env_key = os.environ.get("REPLAY_HMAC_KEY")
    if env_key:
        key = env_key.encode("utf-8")
        if len(key) < 16:
            raise ValueError("REPLAY_HMAC_KEY must be at least 16 bytes")
        return key

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        key = path.read_bytes().strip()
        if len(key) >= 32:
            return key
    key = secrets.token_bytes(32)
    # Atomic write, owner-only permissions.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(key)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return key


def _digest(key: bytes, payload: Any) -> str:
    return hmac.new(key, canonical_json(payload), hashlib.sha256).hexdigest()


def sign_payload(payload: dict[str, Any], key: bytes) -> dict[str, Any]:
    """Return the signed envelope (never mutates the payload)."""
    body = json.loads(json.dumps(payload))  # deep copy via JSON
    body = dict(body)
    body["sig_alg"] = SIGNATURE_ALG
    sig = _digest(key, body)
    return {PAYLOAD_FIELD: body, SIGNATURE_FIELD: sig}


def verify_envelope(envelope: dict[str, Any], key: bytes) -> dict[str, Any]:
    """Validate an envelope produced by :func:`sign_payload`.

    Raises :class:`CheckpointSignatureError` on any tampering, including the
    embedded algorithm tag.
    """
    if not isinstance(envelope, dict):
        raise CheckpointSignatureError("checkpoint envelope is not an object")
    payload = envelope.get(PAYLOAD_FIELD)
    signature = envelope.get(SIGNATURE_FIELD)
    if not isinstance(payload, dict) or not isinstance(signature, str):
        raise CheckpointSignatureError("checkpoint envelope missing payload/signature")
    if payload.get("sig_alg") != SIGNATURE_ALG:
        raise CheckpointSignatureError("unsupported signature algorithm")

    expected = _digest(key, payload)
    if not hmac.compare_digest(expected, signature):
        raise CheckpointSignatureError("checkpoint HMAC does not match")
    return dict(payload)
