"""Shared test helpers."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.config import Settings
from app.corpus.fixtures import load_fixture
from app.corpus.schema import CorpusSpec
from app.mining.kernel import PrefixGrowthMiner
from app.models.domain import GapConstraints, PatternResult


def run_fixture_case(fixture: dict, case: dict) -> dict[str, PatternResult]:
    """Run the kernel over one fixture case; return pattern-string -> result."""
    spec = CorpusSpec(**fixture["corpus"])
    params = case["params"]
    miner = PrefixGrowthMiner(
        spec.to_domain(),
        GapConstraints(
            max_pos_gap=params.get("max_pos_gap"),
            max_time_gap=params.get("max_time_gap"),
        ),
        min_support=params["min_support"],
    )
    return {">".join(r.pattern): r for r in miner.mine()}


def embeddings_of(result: PatternResult) -> dict[str, list[list[int]]]:
    grouped: dict[str, list[list[int]]] = {}
    for ev in result.embeddings:
        grouped.setdefault(ev.sequence_id, []).append(list(ev.positions))
    return {sid: sorted(embs) for sid, embs in grouped.items()}


@pytest.fixture()
def client():
    settings = Settings(
        db_path=":memory:",
        max_pattern_len=8,
        max_embeddings_per_sequence=64,
        log_level="INFO",
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def repeated_events_corpus() -> dict:
    return load_fixture("repeated_events")["corpus"]
