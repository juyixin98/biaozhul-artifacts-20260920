"""Snapshot persistence with a real cryptographic integrity record.

Each snapshot is a JSON document accompanied by:
  * rules_version — which rules engine judged it
  * rules_hash    — sha256 of the exact rules module file at judgement time
                    (detects that a "1.0.0" string was reused after edits)
  * content_sha256 — digest of the canonical snapshot body
  * hmac_sha256   — keyed MAC over that digest, key from QOSDIAG_SNAPSHOT_KEY
                    or a generated per-install key (data/snapshots/.key, 0600)

Verification recomputes both hashes and the HMAC, and reports *which* check
failed — so tampering vs. rule drift are distinguishable.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from .qos_model import RULES_VERSION

_RULES_FILE = Path(__file__).with_name("rules.py")


def canonical_digest(body: dict[str, Any]) -> str:
    # sort_keys + fixed separators -> byte-stable canonical form
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def rules_file_hash() -> str:
    return hashlib.sha256(_RULES_FILE.read_bytes()).hexdigest()


def _load_or_create_key(keyfile: Path) -> bytes:
    env = os.environ.get("QOSDIAG_SNAPSHOT_KEY")
    if env:
        return env.encode("utf-8")
    keyfile.parent.mkdir(parents=True, exist_ok=True)
    if keyfile.exists():
        return keyfile.read_bytes()
    key = secrets.token_bytes(32)
    keyfile.write_bytes(key)
    keyfile.chmod(0o600)
    return key


class SnapshotStore:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._key = _load_or_create_key(self.dir / ".key")

    def _sign(self, digest_hex: str) -> str:
        return hmac.new(self._key, digest_hex.encode("ascii"), hashlib.sha256).hexdigest()

    def save(self, body: dict[str, Any]) -> dict[str, Any]:
        """Wrap a topology/diagnosis body with version + hashes and write it."""
        content_hash = canonical_digest(body)
        record = {
            "snapshot_id": f"snap-{int(time.time()*1000)}-{secrets.token_hex(3)}",
            "captured_ts": time.time(),
            "rules_version": RULES_VERSION,
            "rules_hash": rules_file_hash(),
            "rules_hash_algo": "sha256",
            "content_sha256": content_hash,
            "hmac_algo": "hmac-sha256",
            "body": body,
        }
        record["hmac_sha256"] = self._sign(content_hash)
        path = self.dir / f"{record['snapshot_id']}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False))
        os.replace(tmp, path)  # atomic on POSIX
        return record

    def load(self, snapshot_id: str) -> dict[str, Any]:
        path = self.dir / f"{snapshot_id}.json"
        return json.loads(path.read_text())

    def list(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.dir.glob("snap-*.json")):
            try:
                rec = json.loads(p.read_text())
                out.append({
                    "snapshot_id": rec.get("snapshot_id"),
                    "captured_ts": rec.get("captured_ts"),
                    "rules_version": rec.get("rules_version"),
                    "rules_hash": (rec.get("rules_hash") or "")[:16],
                    "content_sha256": (rec.get("content_sha256") or "")[:16],
                    "hmac_sha256": (rec.get("hmac_sha256") or "")[:16],
                })
            except (json.JSONDecodeError, OSError):
                continue
        return out

    def verify(self, rec: dict[str, Any]) -> dict[str, Any]:
        """Recompute every integrity field; report all failures, not just first."""
        checks: dict[str, Any] = {}
        body = rec.get("body")
        recomputed = canonical_digest(body) if isinstance(body, dict) else None
        checks["content_sha256"] = {
            "ok": recomputed == rec.get("content_sha256"),
            "expected": rec.get("content_sha256"),
            "recomputed": recomputed,
        }
        expected_mac = rec.get("hmac_sha256")
        recomputed_mac = self._sign(recomputed) if recomputed else None
        checks["hmac_sha256"] = {
            "ok": bool(expected_mac) and hmac.compare_digest(
                expected_mac or "", recomputed_mac or ""),
            "expected": expected_mac,
            "recomputed": recomputed_mac,
        }
        current_rules_hash = rules_file_hash()
        checks["rules_hash"] = {
            "ok": rec.get("rules_hash") == current_rules_hash,
            "snapshot_rules_hash": rec.get("rules_hash"),
            "current_rules_hash": current_rules_hash,
            "rules_version": rec.get("rules_version"),
            "note": "mismatch means this snapshot was judged by different rule "
                    "logic than the code currently installed",
        }
        checks["all_ok"] = all(
            v["ok"] for k, v in checks.items() if isinstance(v, dict))
        return checks
