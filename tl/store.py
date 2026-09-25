"""Persistent append-only entry store.

Entries are raw client bytes.  On disk each entry is one JSON line:

    {"index": <int>, "data_b64": "<base64>", "ts": "<utc iso8601>"}

The file is append-only and fsynced per write; on restart the log is
reloaded and hashed.  Nothing is ever rewritten in place.
"""

from __future__ import annotations

import base64
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from . import merkle

MAX_ENTRY_BYTES = 1 * 1024 * 1024  # local safety cap, 1 MiB/entry


class EntryTooLarge(ValueError):
    """Raised when a submitted entry exceeds MAX_ENTRY_BYTES."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass
class Entry:
    index: int
    data: bytes
    ts: str


class LogStore:
    """In-memory leaf list backed by an append-only JSONL file."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._entries: List[Entry] = []
        self._leaf_hashes: List[bytes] = []
        self._load()

    # -- persistence ------------------------------------------------------

    def _load(self) -> None:
        if not os.path.exists(self.path):
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    idx = int(rec["index"])
                    data = base64.b64decode(rec["data_b64"], validate=True)
                    ts = str(rec.get("ts", ""))
                except (KeyError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"corrupt log file {self.path}:{line_no}: {exc}"
                    ) from exc
                if idx != len(self._entries):
                    raise ValueError(
                        f"non-contiguous index at {self.path}:{line_no}: "
                        f"{idx} != {len(self._entries)}"
                    )
                self._entries.append(Entry(idx, data, ts))
                self._leaf_hashes.append(merkle.leaf_hash(data))

    # -- append -----------------------------------------------------------

    def append(self, data: bytes) -> int:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("entry data must be bytes")
        data = bytes(data)
        if len(data) > MAX_ENTRY_BYTES:
            raise EntryTooLarge(
                f"entry is {len(data)} bytes, max is {MAX_ENTRY_BYTES}"
            )
        with self._lock:
            idx = len(self._entries)
            rec = {
                "index": idx,
                "data_b64": base64.b64encode(data).decode("ascii"),
                "ts": _now_iso(),
            }
            # Atomic append: write to temp then... appends to JSONL are small;
            # a direct fsynced append is sufficient and preserves append-only
            # semantics.  We still write line-by-line under the lock.
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._entries.append(Entry(idx, data, rec["ts"]))
            self._leaf_hashes.append(merkle.leaf_hash(data))
            return idx

    # -- reads ------------------------------------------------------------

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def entry(self, index: int) -> Optional[Entry]:
        with self._lock:
            if 0 <= index < len(self._entries):
                return self._entries[index]
            return None

    def leaf_hashes(self) -> List[bytes]:
        with self._lock:
            return list(self._leaf_hashes)

    def root_at(self, size: int) -> bytes:
        """Root hash of the first ``size`` entries (0 <= size <= current)."""
        with self._lock:
            if not (0 <= size <= len(self._leaf_hashes)):
                raise IndexError(f"size {size} out of range")
            return merkle.tree_hash(self._leaf_hashes[:size])
