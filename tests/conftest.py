"""Shared pytest fixtures: isolated SQLite store with the graph fixture."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from provenance.diagnostics import JsonDiagnostics
from provenance.evidence_store import EvidenceStore, Snapshot
from provenance.planner import Planner
from provenance.rule_language import parse_plan

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "graph_v1.json"


@pytest.fixture
def log_stream() -> io.StringIO:
    return io.StringIO()


@pytest.fixture
def diagnostics(log_stream) -> JsonDiagnostics:
    return JsonDiagnostics("provenance.test", stream=log_stream)


@pytest.fixture
def store(tmp_path) -> EvidenceStore:
    db = EvidenceStore(tmp_path / "test.db")
    snapshot = Snapshot.from_dict(json.loads(FIXTURE_PATH.read_text()))
    db.load_snapshot(snapshot)
    yield db
    db.close()


@pytest.fixture
def planner(store, diagnostics) -> Planner:
    return Planner(store, diagnostics)


@pytest.fixture
def graph_plan():
    def _build(plan: dict) -> dict:
        return plan

    return _build


__all__ = ["parse_plan"]
