"""Unit tests for the QueryService validation boundary (no HTTP)."""

from __future__ import annotations

from pathlib import Path

import pytest

from wfst_service.api.service import QueryService
from wfst_service.config import Settings
from wfst_service.index.repository import IndexRepository
from wfst_service.index.service import IndexService

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "corpora"


@pytest.fixture
def query_service() -> QueryService:
    repo = IndexRepository(":memory:")
    index = IndexService(repo)
    index.load_corpus_file("demo", FIXTURE / "demo_corpus.json")
    settings = Settings(
        db_path=":memory:", default_k=5, max_k=50,
        default_budget=10_000, max_budget=50_000,
        max_input_length=8, host="x", port=0, log_level="ERROR",
    )
    return QueryService(index, settings)


@pytest.mark.unit
def test_input_too_long_fails_with_query_error(query_service: QueryService) -> None:
    outcome = query_service.execute(
        "demo", "morphology", "abcdefghij", run_id="r-long"
    )
    assert outcome.status == "error"
    assert outcome.error_code == "query_error"
    assert outcome.run_id == "r-long"


@pytest.mark.unit
def test_unknown_corpus_is_not_found(query_service: QueryService) -> None:
    outcome = query_service.execute(
        "nope", "morphology", "cat", run_id="r-nf"
    )
    assert outcome.error_code == "not_found"


@pytest.mark.unit
def test_epsilon_literal_in_input_rejected(query_service: QueryService) -> None:
    outcome = query_service.execute(
        "demo", "morphology", "ca<eps>", run_id="r-eps"
    )
    assert outcome.error_code == "query_error"


@pytest.mark.unit
def test_oversized_budget_rejected(query_service: QueryService) -> None:
    outcome = query_service.execute(
        "demo", "morphology", "cat", budget=10_000_000, run_id="r-b"
    )
    assert outcome.error_code == "query_error"


@pytest.mark.unit
def test_successful_outcome_is_persisted(query_service: QueryService) -> None:
    outcome = query_service.execute(
        "demo", "correct_then_morph", "kat", run_id="r-ok"
    )
    assert outcome.status == "ok"
    assert outcome.result is not None
    assert outcome.result.outputs[0].output == "kat"
    stored = query_service.repository.get_run("r-ok")
    assert stored is not None and stored.status == "ok"
    assert stored.complete is True


@pytest.mark.unit
def test_budget_outcome_is_incomplete(query_service: QueryService) -> None:
    outcome = query_service.execute(
        "demo", "char_correction", "a", k=50, budget=10, run_id="r-bud"
    )
    assert outcome.status == "budget_exhausted"
    assert outcome.error_code == "budget_exhausted"
    stored = query_service.repository.get_run("r-bud")
    assert stored is not None and stored.status == "budget_exhausted"
    assert stored.complete is False
