"""In-memory immutable snapshot store.

Snapshots are keyed by the SHA-256 digest of their canonical JSON encoding
(separators compact, keys sorted, UTF-8). The digest is computed with the
real :mod:`hashlib` implementation — no stubbing. Stored snapshots are frozen
pydantic models and must not be mutated after upload.
"""

from __future__ import annotations

import hashlib
import json
import threading

from .analyzer import Analyzer
from .models import Snapshot


class SnapshotStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._analyzers: dict[str, Analyzer] = {}

    @staticmethod
    def content_id(raw: dict) -> str:
        canonical = json.dumps(
            raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def put(self, raw: dict) -> tuple[str, Analyzer, bool]:
        """Validate, freeze and store a snapshot.

        Returns (id, analyzer, created). Re-posting identical content returns
        the same id with ``created=False``.
        """
        snapshot = Snapshot.model_validate(raw)
        sid = self.content_id(raw)
        with self._lock:
            existing = self._analyzers.get(sid)
            if existing is not None:
                return sid, existing, False
            analyzer = Analyzer(snapshot)
            self._analyzers[sid] = analyzer
            return sid, analyzer, True

    def get(self, sid: str) -> Analyzer:
        with self._lock:
            analyzer = self._analyzers.get(sid)
        if analyzer is None:
            raise KeyError(sid)
        return analyzer

    def list_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._analyzers)


# One store per API worker process. (With multiple uvicorn --workers each
# process owns its own store; upload snapshots to the worker serving you, or
# run a single worker — see README.)
store = SnapshotStore()
