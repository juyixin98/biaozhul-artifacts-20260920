"""Append-only evidence log with a SHA-256 hash chain and per-record HMAC.

Every state-changing operation (session create, ingest, estimate,
finalize, late-data rejection) is recorded as one JSON line containing:

    seq, event_type, timestamp_s, session_id, body_hash, prev_hash,
    hash = sha256(seq|event|ts|session|body_hash|prev_hash),
    hmac = HMAC-SHA256(evidence_key, line_without_hmac)

Verification walks the file, recomputes the chain and the HMACs, and
fails loudly on any inserted, deleted or modified record.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

GENESIS_HASH = "0" * 64


class EvidenceTamperError(RuntimeError):
    """Raised when the evidence chain fails verification."""


class EvidenceLog:
    def __init__(self, path: Path, hmac_key: bytes):
        self.path = Path(path)
        self._hmac_key = hmac_key
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()
        self._last_hash, self._seq = self._head_state()

    def _head_state(self) -> tuple[str, int]:
        last_hash = GENESIS_HASH
        seq = -1
        for record in self._iter_raw():
            seq = int(record["seq"])
            last_hash = record["hash"]
        return last_hash, seq

    def _iter_raw(self):
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if line:
                    yield json.loads(line)

    @staticmethod
    def _chain_hash(seq: int, event_type: str, ts: float, session_id: str,
                    body_hash: str, prev_hash: str) -> str:
        payload = f"{seq}|{event_type}|{ts}|{session_id}|{body_hash}|{prev_hash}".encode()
        return hashlib.sha256(payload).hexdigest()

    def append(self, event_type: str, session_id: str, body: Any) -> dict:
        with self._lock:
            self._seq += 1
            seq = self._seq
            ts = time.time()
            body_hash = hashlib.sha256(
                json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            h = self._chain_hash(seq, event_type, ts, session_id, body_hash, self._last_hash)
            record = {
                "seq": seq,
                "event_type": event_type,
                "timestamp_s": ts,
                "session_id": session_id,
                "body_hash": body_hash,
                "prev_hash": self._last_hash,
                "hash": h,
            }
            record["hmac"] = hmac.new(
                self._hmac_key,
                json.dumps(record, sort_keys=True, separators=(",", ":")).encode(),
                hashlib.sha256,
            ).hexdigest()
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, sort_keys=True) + "\n")
                f.flush()
            self._last_hash = h
            return record

    def verify(self) -> dict:
        """Recompute the full chain and every HMAC. Returns a summary."""
        prev = GENESIS_HASH
        count = 0
        expected_seq = 0
        with self._lock:
            for record in self._iter_raw():
                provided_hmac = record.get("hmac")
                expected_hmac = hmac.new(
                    self._hmac_key,
                    json.dumps(
                        {k: v for k, v in record.items() if k != "hmac"},
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode(),
                    hashlib.sha256,
                ).hexdigest()
                if not hmac.compare_digest(provided_hmac or "", expected_hmac):
                    raise EvidenceTamperError(f"HMAC mismatch at seq {record.get('seq')}")
                if record["seq"] != expected_seq:
                    raise EvidenceTamperError(
                        f"seq gap: expected {expected_seq}, got {record['seq']}"
                    )
                expected_seq += 1
                if record["prev_hash"] != prev:
                    raise EvidenceTamperError(f"broken prev_hash link at seq {record['seq']}")
                h = self._chain_hash(
                    record["seq"],
                    record["event_type"],
                    record["timestamp_s"],
                    record["session_id"],
                    record["body_hash"],
                    record["prev_hash"],
                )
                if not hmac.compare_digest(h, record["hash"]):
                    raise EvidenceTamperError(f"chain hash mismatch at seq {record['seq']}")
                prev = record["hash"]
                count += 1
        return {"records": count, "last_hash": prev, "ok": True}
