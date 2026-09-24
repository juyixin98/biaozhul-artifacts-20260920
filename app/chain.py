"""Hash-chain construction for records and checkpoints.

Record layout (stored, also exported verbatim)::

    {
      "seq": 3, "ts": "2026-09-24T13:00:00.000000+00:00",
      "actor": "alice", "action": "login", "resource": "/sessions",
      "payload": {... arbitrary JSON ...},
      "prev_hash": "<sha256-hex of record seq-1>",
      "hash": "<sha256-hex of canonical body of THIS record>"
    }

The signed/hashed body is the record WITHOUT the self-referential "hash".
record 1 chains to GENESIS_HASH (64 zero hex chars).

Checkpoint layout::

    {
      "version": "ed25519-v1",
      "seq": 10,                       # log length covered
      "record_hash": "<head record hash, or GENESIS when seq==0>",
      "ts": "...",
      "prev_checkpoint_hash": "<sha256-hex of previous checkpoint body>",
      "signature": "<hex ed25519 over canonical checkpoint body>"
    }

A checkpoint with seq == 0 over GENESIS_HASH is a "genesis anchor": it lets a
verifier prove the log is genuinely empty rather than merely truncated.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import keys as keymod
from .canonical import GENESIS_CHECKPOINT_HASH, GENESIS_HASH, canonical, hash_canonical

RECORD_BODY_FIELDS = (
    "seq",
    "ts",
    "actor",
    "action",
    "resource",
    "payload",
    "prev_hash",
)

CHECKPOINT_BODY_FIELDS = (
    "seq",
    "record_hash",
    "ts",
    "prev_checkpoint_hash",
)


def utc_now() -> str:
    """UTC timestamp in ISO-8601 with explicit offset (timezone-aware)."""
    return datetime.now(timezone.utc).isoformat()


def record_body(record: dict[str, Any]) -> dict[str, Any]:
    """Extract the signed/hashed fields of a record."""
    return {f: record[f] for f in RECORD_BODY_FIELDS}


def build_record(
    *,
    seq: int,
    prev_hash: str,
    actor: str,
    action: str,
    resource: str,
    payload: Any,
    ts: str | None = None,
) -> dict[str, Any]:
    rec = {
        "seq": seq,
        "ts": ts or utc_now(),
        "actor": actor,
        "action": action,
        "resource": resource,
        "payload": payload,
        "prev_hash": prev_hash,
    }
    rec["hash"] = hash_canonical(record_body(rec))
    return rec


def record_hash(record: dict[str, Any]) -> str:
    return hash_canonical(record_body(record))


def checkpoint_body(cp: dict[str, Any]) -> dict[str, Any]:
    return {f: cp[f] for f in CHECKPOINT_BODY_FIELDS}


def checkpoint_message(cp: dict[str, Any]) -> bytes:
    return canonical(checkpoint_body(cp))


def checkpoint_hash(cp: dict[str, Any]) -> str:
    return hash_canonical(checkpoint_body(cp))


def build_checkpoint(
    *,
    seq: int,
    record_hash: str,
    signing_key: Ed25519PrivateKey,
    prev_checkpoint_hash: str = GENESIS_CHECKPOINT_HASH,
    ts: str | None = None,
) -> dict[str, Any]:
    cp: dict[str, Any] = {
        "seq": seq,
        "record_hash": record_hash,
        "ts": ts or utc_now(),
        "prev_checkpoint_hash": prev_checkpoint_hash,
    }
    sig = keymod.sign(signing_key, canonical(cp))
    cp_out = {"version": keymod.KEY_VERSION, **cp, "signature": sig.hex()}
    return cp_out
