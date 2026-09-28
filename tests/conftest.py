"""Shared pytest fixtures: in-memory service, API client, corpus loaders."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.config import Settings
from app.corpus.loader import load_fixture
from app.index.db import Store
from app.service import AuditService

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def settings() -> Settings:
    return Settings(db_path=":memory:")


@pytest.fixture()
def service(settings: Settings) -> AuditService:
    svc = AuditService(Store(":memory:"), settings)
    yield svc
    svc.store.close()


@pytest.fixture()
def client(settings: Settings) -> TestClient:
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def load_corpus(service: AuditService):
    """Load a fixture file by name; returns the corpus_id."""

    def _load(name: str) -> int:
        corpus_name, transactions = load_fixture(FIXTURES / f"{name}.json")
        return service.create_corpus(corpus_name, transactions).corpus_id

    return _load


def fixture_transactions(name: str) -> list[frozenset[str]]:
    """Raw fixture transactions as deduplicated frozensets (for reference_impl)."""
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return [frozenset(t["items"]) for t in data["transactions"]]
