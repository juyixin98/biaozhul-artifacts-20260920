"""Integration tests: SQLite index and the corpus-loading service."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wfst_service.corpus.errors import CycleError, NotFoundError, SpecError
from wfst_service.index.repository import IndexRepository
from wfst_service.index.service import COMPOSED_PREFIX, IndexService

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "corpora"


@pytest.fixture
def service() -> tuple[IndexService, IndexRepository]:
    repo = IndexRepository(":memory:")
    return IndexService(repo), repo


@pytest.mark.integration
def test_demo_corpus_loads_and_materialises_pipeline(
    service: tuple[IndexService, IndexRepository]
) -> None:
    svc, repo = service
    report = svc.load_corpus_file("demo", FIXTURE / "demo_corpus.json")
    names = {m.name for m in report.models}
    assert "char_correction" in names
    assert "morphology" in names
    assert "lex_general" in names
    assert f"{COMPOSED_PREFIX}correct_then_morph" in names

    # Persisted transducers are retrievable and pipelines resolve to the
    # precomposed model.
    fst, kind = svc.resolve_model("demo", "correct_then_morph")
    assert kind == "pipeline"
    assert fst.name == f"{COMPOSED_PREFIX}correct_then_morph"

    fst2, kind2 = svc.resolve_model("demo", "char_correction")
    assert kind2 == "transducer"
    assert fst2 is not None

    listed = repo.list_transducers("demo")
    assert {"fst", "composed"} <= {row["kind"] for row in listed}


@pytest.mark.integration
def test_unknown_corpus_and_target_fail_with_not_found(
    service: tuple[IndexService, IndexRepository]
) -> None:
    svc, _ = service
    svc.load_corpus_file("demo", FIXTURE / "demo_corpus.json")
    with pytest.raises(NotFoundError) as exc:
        svc.resolve_model("ghost", "char_correction")
    assert exc.value.code == "not_found"

    with pytest.raises(NotFoundError):
        svc.resolve_model("demo", "no_such_target")


@pytest.mark.integration
@pytest.mark.parametrize(
    "filename,witnessed_code",
    [
        ("invalid_epsilon_cycle.json", "epsilon"),
        ("invalid_negative_cycle.json", "negative"),
    ],
)
def test_forbidden_cycles_rejected_at_load(
    service: tuple[IndexService, IndexRepository],
    filename: str,
    witnessed_code: str,
) -> None:
    svc, repo = service
    with pytest.raises(CycleError) as exc:
        svc.load_corpus_file("bad", FIXTURE / filename)
    assert exc.value.states  # a witness is always supplied
    message = str(exc.value)
    if witnessed_code == "epsilon":
        assert "epsilon cycle" in message
    else:
        assert "negative-cost cycle" in message
    # Nothing must have been persisted for the rejected corpus.
    assert repo.get_corpus("bad") is None


@pytest.mark.integration
def test_malformed_document_is_spec_error_not_runtime_error(
    service: tuple[IndexService, IndexRepository]
) -> None:
    svc, _ = service
    with pytest.raises(SpecError) as exc:
        svc.load_corpus_json("bad", {"version": 1, "transducers": []})
    assert exc.value.code == "spec_error"


@pytest.mark.integration
def test_reload_replaces_materialised_models(
    service: tuple[IndexService, IndexRepository]
) -> None:
    svc, repo = service
    svc.load_corpus_file("demo", FIXTURE / "demo_corpus.json")
    first = len(repo.list_transducers("demo"))
    svc.load_corpus_file("demo", FIXTURE / "demo_corpus.json")
    second = len(repo.list_transducers("demo"))
    assert first == second  # no duplication after reload
