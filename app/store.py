"""Append-only log storage.

Two files live in a data directory:

* ``records.jsonl``  - one canonical JSON record per line, append-only
* ``checkpoints.jsonl`` - one signed checkpoint per line, append-only

Writes are serialized by a threading.Lock (FastAPI sync endpoints run in a
threadpool). Each append is flushed and fsynced before returning so a crash
cannot lose an acknowledged record silently.

No verification key is ever stored here; the private signing key is loaded
from a separate PEM file (see app.keys / AUDIT_SIGNING_KEY).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import chain
from .canonical import GENESIS_CHECKPOINT_HASH, GENESIS_HASH


def _dump_line(obj: dict[str, Any]) -> str:
    # Storage uses the same canonical encoder as hashing, so a stored line is
    # byte-identical to what the verifier reconstructs.
    from .canonical import canonical

    return canonical(obj).decode("utf-8")


class AuditStore:
    def __init__(self, data_dir: str | Path, signing_key: Ed25519PrivateKey):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.records_path = self.data_dir / "records.jsonl"
        self.checkpoints_path = self.data_dir / "checkpoints.jsonl"
        self.signing_key = signing_key
        self._lock = threading.RLock()
        self.records: list[dict[str, Any]] = []
        self.checkpoints: list[dict[str, Any]] = []
        self._load()

    # ------------------------------------------------------------------ load
    def _load(self) -> None:
        self.records = [
            json.loads(line)
            for line in self._read_lines(self.records_path)
        ]
        self.checkpoints = [
            json.loads(line)
            for line in self._read_lines(self.checkpoints_path)
        ]

    @staticmethod
    def _read_lines(path: Path) -> list[str]:
        if not path.exists():
            return []
        lines = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    lines.append(line)
        return lines

    # ----------------------------------------------------------------- stats
    @property
    def length(self) -> int:
        return len(self.records)

    def head_hash(self) -> str:
        return self.records[-1]["hash"] if self.records else GENESIS_HASH

    def latest_checkpoint(self) -> dict[str, Any] | None:
        return self.checkpoints[-1] if self.checkpoints else None

    # --------------------------------------------------------------- appends
    def append(
        self,
        *,
        actor: str,
        action: str,
        resource: str,
        payload: Any,
        ts: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            seq = len(self.records) + 1
            prev_hash = self.head_hash()
            record = chain.build_record(
                seq=seq,
                prev_hash=prev_hash,
                actor=actor,
                action=action,
                resource=resource,
                payload=payload,
                ts=ts,
            )
            self._append_line(self.records_path, record)
            self.records.append(record)
            return record

    def _append_line(self, path: Path, obj: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(_dump_line(obj) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    # ------------------------------------------------------------- checkpoints
    def create_checkpoint(self, *, ts: str | None = None, force: bool = False) -> dict[str, Any]:
        """Sign the current chain head.

        By default this is a no-op (returning the existing checkpoint) when no
        record has been appended since the last one, so a periodic signer does
        not litter the log with identical-head checkpoints. ``force=True``
        always appends (used by the explicit POST /checkpoints endpoint)."""
        with self._lock:
            head = self.head_hash()
            latest = self.checkpoints[-1] if self.checkpoints else None
            if latest is not None and not force and latest["record_hash"] == head:
                return latest
            prev = (
                chain.checkpoint_hash(latest)
                if latest is not None
                else GENESIS_CHECKPOINT_HASH
            )
            cp = chain.build_checkpoint(
                seq=len(self.records),
                record_hash=head,
                signing_key=self.signing_key,
                prev_checkpoint_hash=prev,
                ts=ts,
            )
            self._append_line(self.checkpoints_path, cp)
            self.checkpoints.append(cp)
            return cp

    def ensure_genesis_anchor(self) -> dict[str, Any]:
        """Create the seq=0 anchor on an empty log if none exists yet."""
        with self._lock:
            if not self.checkpoints:
                return self.create_checkpoint()
            return self.checkpoints[0]

    # --------------------------------------------------------------- reads
    def list_records(
        self, start: int | None = None, end: int | None = None
    ) -> list[dict[str, Any]]:
        """Return records with seq in the inclusive [start, end] window.

        Missing bounds mean open-ended. seq values are 1-based."""
        with self._lock:
            lo = 1 if start is None else max(1, start)
            hi = len(self.records) if end is None else min(len(self.records), end)
            if lo > hi:
                return []
            return [dict(r) for r in self.records[lo - 1 : hi]]

    def export(
        self, start: int | None = None, end: int | None = None
    ) -> dict[str, Any]:
        """Build an export bundle for the inclusive range.

        The bundle always includes the full checkpoint list (needed to find a
        trust anchor) plus the requested record slice and metadata the
        verifier cross-checks rather than trusts.
        """
        with self._lock:
            total = len(self.records)
            lo = 1 if start is None else max(1, start)
            hi = total if end is None else min(total, end)
            slice_records = (
                [dict(r) for r in self.records[lo - 1 : hi]] if lo <= hi else []
            )
            return {
                "format": "audit-export-v1",
                "exported_at": chain.utc_now(),
                "total_records": total,
                "start": lo,
                "end": hi,
                "includes_tail": hi >= total and total > 0 or (hi == 0 and total == 0),
                "records": slice_records,
                "checkpoints": [dict(c) for c in self.checkpoints],
            }
