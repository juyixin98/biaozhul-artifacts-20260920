"""Shared pytest fixtures: synthetic local data only, no external accounts."""
from __future__ import annotations

import json
import os
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from app.config import Settings
from app.repository import Database
from app.service import RuleAuditService

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def canonical_data() -> Dict[str, Any]:
    with open(FIXTURE_DIR / "canonical_corpus.json", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="session")
def expected_metrics_data() -> Dict[str, Any]:
    with open(FIXTURE_DIR / "expected_metrics.json", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture()
def raw_transactions(canonical_data: Dict[str, Any]) -> List[List[str]]:
    return [list(row) for row in canonical_data["raw_transactions"]]


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "test.db"),
        small_sample_threshold=30,
        rare_event_threshold=5,
        max_transactions=100_000,
        max_items_per_transaction=1_000,
        redact_pii=True,
    )


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(str(tmp_path / "test.db"))
    yield database
    database.close()


@pytest.fixture()
def service(db: Database, settings: Settings) -> RuleAuditService:
    return RuleAuditService(db, settings)


@pytest.fixture()
def ingested(
    service: RuleAuditService, raw_transactions: List[List[str]]
) -> Tuple[RuleAuditService, int]:
    result = service.ingest_dataset(
        "canonical", raw_transactions, min_support=0.2, overwrite=True
    )
    return service, result.dataset_id


def frac(pair: List[int]) -> Fraction:
    return Fraction(pair[0], pair[1])
