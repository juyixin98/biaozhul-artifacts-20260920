"""Append-only, hash-chained evidence log (JSONL).

Each record carries ``seq``, ``prev_hash`` and ``hash`` where
``hash = SHA-256(canonical(record without hash))`` and the canonical form
includes ``prev_hash``. Tampering with, deleting, or reordering any line
breaks the chain and is detected by :func:`verify_chain`. This is real
SHA-256 chaining, not a mock.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .util import canonical_bytes

GENESIS_HASH = "0" * 64


def _record_hash(record: dict[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def append_record(path: str | Path, record: dict[str, Any]) -> dict[str, Any]:
    """Append a record to the chain file, returning the stored record."""
    path = Path(path)
    records = read_records(path)
    seq = len(records)
    prev_hash = records[-1]["hash"] if records else GENESIS_HASH
    stored = {"seq": seq, "prev_hash": prev_hash, **record}
    stored["hash"] = _record_hash(stored)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(stored, sort_keys=True) + "\n")
    return stored


def read_records(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def verify_chain(path: str | Path) -> bool:
    """Recompute the whole chain; True iff every link and hash matches."""
    prev_hash = GENESIS_HASH
    for expected_seq, record in enumerate(read_records(path)):
        if record.get("seq") != expected_seq:
            return False
        if record.get("prev_hash") != prev_hash:
            return False
        if record.get("hash") != _record_hash(record):
            return False
        prev_hash = record["hash"]
    return True
