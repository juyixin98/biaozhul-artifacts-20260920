"""Checkpoint save / restore with cryptographic sealing.

A checkpoint captures:

* ``bag`` -- the full :class:`BagIndex` digest summary (file sizes+SHA-256,
  message index hash, timestamps, counts, topics), so any change of the
  source files is detected;
* ``topic_filter`` and ``position`` (next stable seq, rate, generation);
* creation metadata and a fresh checkpoint id.

On restore the on-disk files are re-hashed and, for safety, the bag is fully
re-indexed and compared against ``bag.message_index_sha256``. Mismatches of
either kind raise :class:`SourceChangedError` and the session is not touched.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from . import bagstore
from .bagstore import BagIndex, CorruptBagError
from .crypto import (
    CheckpointSignatureError,
    canonical_json,
    sign_payload,
    verify_envelope,
)

CHECKPOINT_FORMAT = "ros-replay-checkpoint/v1"


class SourceChangedError(RuntimeError):
    """Checkpoint bag summary does not match the current on-disk source."""


class CheckpointStore:
    def __init__(self, state_dir: Path, key: bytes) -> None:
        self.dir = state_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._key = key

    # --------------------------------------------------------------- save

    def save(
        self,
        *,
        session_id: str,
        index: BagIndex,
        position: dict[str, Any],
        transport: str,
        checkpoint_id: str | None = None,
    ) -> dict[str, Any]:
        cp_id = checkpoint_id or uuid.uuid4().hex
        payload = {
            "format": CHECKPOINT_FORMAT,
            "checkpoint_id": cp_id,
            "session_id": session_id,
            "created_at": time.time(),
            "transport": transport,
            "bag": index.digest_summary(),
            "position": position,
        }
        envelope = sign_payload(payload, self._key)
        path = self._path(cp_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(envelope, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return {"checkpoint_id": cp_id, "path": str(path), "payload": payload}

    def _path(self, cp_id: str) -> Path:
        safe = "".join(c for c in cp_id if c.isalnum() or c in "-_")
        if safe != cp_id or not safe:
            raise ValueError("invalid checkpoint id")
        return self.dir / f"{safe}.json"

    def list(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                env = json.loads(p.read_text(encoding="utf-8"))
                payload = verify_envelope(env, self._key)
            except (OSError, json.JSONDecodeError, CheckpointSignatureError):
                continue
            out.append(
                {
                    "checkpoint_id": payload.get("checkpoint_id"),
                    "session_id": payload.get("session_id"),
                    "created_at": payload.get("created_at"),
                    "bag_uri": payload.get("bag", {}).get("uri"),
                    "next_seq": payload.get("position", {}).get("next_seq"),
                }
            )
        return out

    # ------------------------------------------------------------- verify

    def load_verified(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Verify signature only (cheap, no disk access)."""
        return verify_envelope(raw, self._key)

    def load_file_verified(self, cp_id: str) -> dict[str, Any]:
        path = self._path(cp_id)
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        return self.load_verified(envelope)

    def restore_check(self, payload: dict[str, Any], *, deep: bool = True) -> BagIndex:
        """Validate a checkpoint against current disk state.

        Returns a freshly built :class:`BagIndex` ready to back a restored
        session, or raises (without side effects) on any mismatch.
        """
        if payload.get("format") != CHECKPOINT_FORMAT:
            raise CheckpointSignatureError(
                f"unsupported checkpoint format: {payload.get('format')!r}"
            )
        summary = payload.get("bag")
        if not isinstance(summary, dict):
            raise CheckpointSignatureError("checkpoint missing bag summary")
        bag_dir = Path(summary["uri"])
        if not bag_dir.is_dir():
            raise SourceChangedError(f"bag directory no longer exists: {bag_dir}")

        # Re-index (opens + reads the bag) -- this also rejects corruption.
        try:
            fresh = bagstore.index_bag(bag_dir)
        except CorruptBagError:
            raise
        current = fresh.digest_summary()

        # Compare the canonical encodings so key order cannot mask a change.
        for field_name in ("files", "message_index_sha256", "total_payload_bytes",
                           "message_count", "starting_time_ns", "duration_ns",
                           "storage_id", "topics"):
            if canonical_json(current[field_name]) != canonical_json(summary[field_name]):
                raise SourceChangedError(
                    f"bag source changed since checkpoint: {field_name} differs"
                )
        if deep and fresh.message_index_sha256 != summary["message_index_sha256"]:
            raise SourceChangedError("message index hash differs")
        return fresh
