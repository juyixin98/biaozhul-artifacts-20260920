"""FastAPI surface for the offline NetworkPolicy reachability analyzer.

The service is a thin, stateless-per-request wrapper around
:func:`app.analyzer.analyze`.  A snapshot is loaded once via ``PUT /snapshot``
and stored as an immutable (frozen pydantic) object; every ``POST /analyze``
query is evaluated against that exact snapshot.
"""

from __future__ import annotations

import hashlib
import threading

from fastapi import FastAPI, HTTPException

from .analyzer import AnalysisError, analyze
from .models import ReachabilityQuery, Snapshot

app = FastAPI(
    title="NetworkPolicy Reachability Analyzer",
    version="1.0.0",
    description="Offline Kubernetes NetworkPolicy reachability analysis.",
)


class _SnapshotStore:
    """Holds the currently loaded immutable snapshot."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None
        self._digest: str | None = None

    def load(self, snapshot: Snapshot) -> str:
        canonical = snapshot.model_dump_json(by_alias=True)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with self._lock:
            self._snapshot = snapshot
            self._digest = digest
        return digest

    def get(self) -> tuple[Snapshot, str]:
        with self._lock:
            if self._snapshot is None or self._digest is None:
                raise HTTPException(
                    status_code=409,
                    detail="no snapshot loaded; PUT /snapshot first",
                )
            return self._snapshot, self._digest

    def reset(self) -> None:
        with self._lock:
            self._snapshot = None
            self._digest = None


_store = _SnapshotStore()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.put("/snapshot")
def load_snapshot(snapshot: Snapshot) -> dict:
    """Load (or replace) the cluster snapshot used by subsequent queries."""
    digest = _store.load(snapshot)
    return {
        "status": "loaded",
        "sha256": digest,
        "namespaces": len(snapshot.namespaces),
        "pods": len(snapshot.pods),
        "policies": len(snapshot.policies),
    }


@app.get("/snapshot")
def snapshot_info() -> dict:
    snapshot, digest = _store.get()
    return {
        "sha256": digest,
        "namespaces": len(snapshot.namespaces),
        "pods": len(snapshot.pods),
        "policies": len(snapshot.policies),
    }


@app.post("/analyze")
def analyze_query(query: ReachabilityQuery) -> dict:
    snapshot, digest = _store.get()
    try:
        result = analyze(snapshot, query)
    except AnalysisError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result["snapshotSha256"] = digest
    return result
