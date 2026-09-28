"""Shared pytest fixtures: isolated DB and log dir per test."""

from __future__ import annotations

from pathlib import Path

import pytest

from entity_resolution.api import create_app
from entity_resolution.clustering import SolverConfig
from entity_resolution.service import EntityResolutionService
from entity_resolution.similarity import SimilarityConfig
from entity_resolution.storage import Storage
from fastapi.testclient import TestClient


@pytest.fixture()
def paths(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def storage(paths: Path) -> Storage:
    return Storage(paths / "er.sqlite3")


@pytest.fixture()
def service(storage: Storage, paths: Path) -> EntityResolutionService:
    return EntityResolutionService(
        storage,
        similarity=SimilarityConfig(threshold=0.6),
        solver=SolverConfig(mode="auto"),
        log_dir=paths / "logs",
    )


@pytest.fixture()
def strict_service(storage: Storage, paths: Path) -> EntityResolutionService:
    """Service treating reg_id as a hard identity attribute."""
    return EntityResolutionService(
        storage,
        similarity=SimilarityConfig(
            threshold=0.6, hard_attribute_keys=frozenset({"reg_id"})
        ),
        solver=SolverConfig(mode="auto"),
        log_dir=paths / "logs",
    )


@pytest.fixture()
def client(service: EntityResolutionService) -> TestClient:
    app = create_app(service=service)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def exact_client(paths: Path) -> TestClient:
    """App whose solver is exhaustive with a tiny partition budget."""
    svc = EntityResolutionService(
        Storage(paths / "exact.sqlite3"),
        similarity=SimilarityConfig(threshold=0.99),
        solver=SolverConfig(mode="exact", max_exact_partitions=100),
        log_dir=paths / "logs",
    )
    app = create_app(service=svc)
    return TestClient(app, raise_server_exceptions=False)
