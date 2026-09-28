"""End-to-end HTTP tests (real FastAPI app + in-memory SQLite).

These drive the full stack -- HTTP validation, index resolution, kernel
search, persistence and correlated run logs -- and assert concrete
results and failure categories, never mere callability.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wfst_service.api import create_app
from wfst_service.config import Settings
from wfst_service.index.repository import IndexRepository

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "corpora"


@pytest.fixture
def client() -> TestClient:
    settings = Settings(
        db_path=":memory:",
        default_k=5,
        max_k=100,
        default_budget=100_000,
        max_budget=5_000_000,
        max_input_length=128,
        host="127.0.0.1",
        port=0,
        log_level="WARNING",
    )
    repo = IndexRepository(":memory:")
    app = create_app(settings=settings, repository=repo)
    return TestClient(app)


@pytest.fixture
def loaded_client(client: TestClient) -> TestClient:
    doc = json.loads((FIXTURE / "demo_corpus.json").read_text())
    resp = client.post("/corpora/demo/load", json=doc)
    assert resp.status_code == 200, resp.text
    return client


@pytest.mark.e2e
def test_health_and_version(loaded_client: TestClient) -> None:
    body = loaded_client.get("/health").json()
    assert body["status"] == "ok"
    assert body["version"]
    assert loaded_client.get("/version").json()["schema"] == "wfst.corpus.v1"


@pytest.mark.e2e
def test_pipeline_query_returns_concrete_ranked_outputs(
    loaded_client: TestClient,
) -> None:
    resp = loaded_client.post(
        "/query",
        json={
            "corpus_id": "demo",
            "target": "correct_then_morph",
            "input": "kat",
            "k": 5,
            "budget": 50_000,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["accepted"] is True
    assert body["complete"] is True
    outputs = [(o["output"], round(o["cost"], 6)) for o in body["outputs"]]
    # Concrete expectation: identity analysis "kat" is cheapest, then the
    # morphological "kats" (+s, 0.3) and "kati"/"katin"/"kating" (+ing...).
    assert outputs[0] == ("kat", 0.0)
    assert outputs[1] == ("kats", 0.3)
    assert outputs == sorted(outputs, key=lambda x: (x[1], x[0]))
    # Logs correlate with the run id and expose the decision basis.
    assert body["logs"], "expected correlated computation logs"
    assert body["run_id"] in body["logs"][0]
    assert body["version"]


@pytest.mark.e2e
def test_corpora_listing_exposes_models_and_pipeline_sequence(
    loaded_client: TestClient,
) -> None:
    rows = loaded_client.get("/corpora").json()
    assert len(rows) == 1
    demo = rows[0]
    assert demo["corpus_id"] == "demo"
    names = {m["name"] for m in demo["transducers"]}
    assert {"char_correction", "morphology", "lex_general"} <= names
    assert demo["pipelines"]["correct_then_morph"] == [
        "char_correction",
        "morphology",
    ]


@pytest.mark.e2e
def test_run_is_persisted_and_retrievable_by_run_id(
    loaded_client: TestClient,
) -> None:
    query = loaded_client.post(
        "/query",
        json={
            "corpus_id": "demo",
            "target": "char_correction",
            "input": "katz",
            "k": 3,
        },
    ).json()
    run_id = query["run_id"]

    stored = loaded_client.get(f"/corpora/demo/runs/{run_id}").json()
    assert stored["status"] == "ok"
    assert stored["input"] == "katz"
    assert stored["complete"] is True
    # Deletion of z costs 1.0 -> cheapest output is "kat" at cost 1.0.
    assert stored["results"][0]["output"] == "kat"
    assert stored["results"][0]["cost"] == pytest.approx(1.0)
    assert any("katz" in line for line in stored["logs"])


@pytest.mark.e2e
def test_no_path_is_200_with_explicit_status_not_a_failure(
    loaded_client: TestClient,
) -> None:
    # '!' is outside every alphabet and has no identity/rule arc.
    resp = loaded_client.post(
        "/query",
        json={
            "corpus_id": "demo",
            "target": "morphology",
            "input": "!",
            "k": 3,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "no_path"
    assert body["accepted"] is False
    assert body["outputs"] == []
    assert "no_path" in body["decision"]


@pytest.mark.e2e
def test_unknown_target_is_404_not_found(loaded_client: TestClient) -> None:
    resp = loaded_client.post(
        "/query",
        json={
            "corpus_id": "demo",
            "target": "ghost",
            "input": "cat",
        },
    )
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "not_found"
    assert resp.json()["complete"] is False


@pytest.mark.e2e
def test_invalid_request_shape_is_422_query_error(
    loaded_client: TestClient,
) -> None:
    resp = loaded_client.post(
        "/query",
        json={"corpus_id": "demo", "target": "morphology", "input": "ab", "k": 0},
    )
    assert resp.status_code == 422


@pytest.mark.e2e
def test_budget_exhaustion_is_503_and_marked_incomplete(
    loaded_client: TestClient,
) -> None:
    # The speller inserts arbitrarily many 'e's (epsilon loop, cost 1.5);
    # asking for far more outputs than the tiny budget allows must fail
    # loudly as incomplete, never as truncated success.
    resp = loaded_client.post(
        "/query",
        json={
            "corpus_id": "demo",
            "target": "char_correction",
            "input": "a",
            "k": 100,
            "budget": 15,
        },
    )
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["error_code"] == "budget_exhausted"
    assert body["complete"] is False
    run_id = body["run_id"]

    stored = loaded_client.get(f"/corpora/demo/runs/{run_id}").json()
    assert stored["status"] == "budget_exhausted"
    assert stored["error_code"] == "budget_exhausted"


@pytest.mark.e2e
def test_epsilon_cycle_corpus_rejected_on_load(client: TestClient) -> None:
    doc = json.loads((FIXTURE / "invalid_epsilon_cycle.json").read_text())
    resp = client.post("/corpora/bad/load", json=doc)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_code"] == "cycle_error"


@pytest.mark.e2e
def test_negative_cycle_corpus_rejected_on_load(client: TestClient) -> None:
    doc = json.loads((FIXTURE / "invalid_negative_cycle.json").read_text())
    resp = client.post("/corpora/bad/load", json=doc)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_code"] == "cycle_error"


@pytest.mark.e2e
def test_malformed_corpus_is_422_spec_error(client: TestClient) -> None:
    resp = client.post(
        "/corpora/bad/load", json={"version": 1, "transducers": []}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_code"] == "spec_error"
