"""Shared pytest fixtures."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings
from app.services import ReasoningService
from app.store import EvidenceStore

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture()
def tmp_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "evidence.db",
        log_path=tmp_path / "service.log",
        model_cap=100_000,
        log_level="INFO",
    )


@pytest.fixture()
def store(tmp_settings: Settings) -> EvidenceStore:
    return EvidenceStore(tmp_settings.db_path)


@pytest.fixture()
def service(tmp_settings: Settings, store: EvidenceStore) -> ReasoningService:
    return ReasoningService(store, model_cap=tmp_settings.model_cap)


@pytest.fixture()
def client(tmp_settings: Settings):
    """FastAPI test client with dependency overrides pointed at tmp storage."""
    from fastapi.testclient import TestClient

    from app.api.deps import get_service, get_settings, get_store
    from app.main import create_app

    app = create_app(tmp_settings)
    svc = ReasoningService(
        EvidenceStore(tmp_settings.db_path), model_cap=tmp_settings.model_cap
    )
    app.dependency_overrides[get_settings] = lambda: tmp_settings
    app.dependency_overrides[get_store] = lambda: svc.store
    app.dependency_overrides[get_service] = lambda: svc
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def load_fixture(name: str) -> dict:
    with (FIXTURE_DIR / f"{name}.json").open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture()
def fixture_payload():
    return load_fixture
