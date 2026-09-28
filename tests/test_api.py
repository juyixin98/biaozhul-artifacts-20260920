"""End-to-end HTTP behavior using FastAPI's TestClient (local, in-process)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("RULE_AUDIT_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("RULE_AUDIT_REDACT_PII", "true")
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def canonical_payload(raw_transactions: List[List[str]]) -> Dict[str, Any]:
    return {"name": "canonical", "transactions": raw_transactions, "min_support": 0.2}


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ingest_then_rules_end_to_end(
    client: TestClient, canonical_payload: Dict[str, Any]
) -> None:
    ingested = client.post("/api/datasets", json=canonical_payload)
    assert ingested.status_code == 201, ingested.text
    body = ingested.json()
    assert body["n_transactions"] == 5
    assert body["n_itemsets"] >= 2
    assert all("request_id" in d for d in body["diagnostics"])

    rules = client.post(
        "/api/datasets/canonical/rules", json={"min_confidence": 0.0}
    )
    assert rules.status_code == 200, rules.text
    rbody = rules.json()
    assert rbody["n_rules"] == len(rbody["rules"])
    assert rbody["request_id"] == rules.headers["x-request-id"]

    # a -> u: confidence 1, lift exactly 1, independence, causation warning.
    a_to_u = next(
        r for r in rbody["rules"]
        if r["antecedent"] == ["a"] and r["consequent"] == ["u"]
    )
    assert a_to_u["metrics"]["confidence"] == pytest.approx(1.0)
    assert a_to_u["metrics"]["lift"] == pytest.approx(1.0)
    assert a_to_u["metrics"]["leverage"] == pytest.approx(0.0)
    assert "lift_not_causation" in a_to_u["warnings"]


def test_request_id_is_honored_and_echoed(
    client: TestClient, canonical_payload: Dict[str, Any]
) -> None:
    client.post("/api/datasets", json=canonical_payload)
    response = client.post(
        "/api/datasets/canonical/rules",
        json={"min_confidence": 0.5},
        headers={"x-request-id": "req-correlated-42"},
    )
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req-correlated-42"
    assert response.json()["request_id"] == "req-correlated-42"


def test_empty_antecedent_returns_typed_422(
    client: TestClient, canonical_payload: Dict[str, Any]
) -> None:
    client.post("/api/datasets", json=canonical_payload)
    response = client.post(
        "/api/datasets/canonical/rules", json={"antecedent": []}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "request_out_of_domain"
    assert body["request_id"]
    reasons = {issue["reason"] for issue in body["issues"]}
    assert "empty_antecedent" in reasons


def test_threshold_out_of_range_rejected_at_boundary(
    client: TestClient, canonical_payload: Dict[str, Any]
) -> None:
    client.post("/api/datasets", json=canonical_payload)
    response = client.post(
        "/api/datasets/canonical/rules", json={"min_confidence": 2.0}
    )
    assert response.status_code == 422


def test_min_confidence_pruning_does_not_drop_rules_silently(
    client: TestClient, canonical_payload: Dict[str, Any]
) -> None:
    client.post("/api/datasets", json=canonical_payload)
    response = client.post(
        "/api/datasets/canonical/rules", json={"min_confidence": 0.9}
    )
    rules = response.json()["rules"]
    rejected = [r for r in rules if r["status"] == "rejected"]
    assert rejected
    assert all(
        any("min_confidence" in why for why in r["reasons"]) for r in rejected
    )


def test_unknown_dataset_is_404(client: TestClient) -> None:
    response = client.post("/api/datasets/missing/rules", json={})
    assert response.status_code == 404
    assert response.json()["request_id"]


def test_min_support_zero_rejected_on_ingest(client: TestClient, raw_transactions: List[List[str]]) -> None:
    response = client.post(
        "/api/datasets",
        json={"name": "bad", "transactions": raw_transactions, "min_support": 0.0},
    )
    assert response.status_code == 422


def test_overlapping_sides_do_not_leak_item_names(
    client: TestClient, canonical_payload: Dict[str, Any]
) -> None:
    client.post("/api/datasets", json=canonical_payload)
    response = client.post(
        "/api/datasets/canonical/rules",
        json={"antecedent": ["a"], "consequent": ["a", "u"]},
    )
    assert response.status_code == 422
    text = response.text
    assert "overlapping_antecedent_consequent" in text
    # Only the structural reason, not item values, is exposed in the detail.
    assert '"a"' not in text


def test_list_datasets_after_ingest(
    client: TestClient, canonical_payload: Dict[str, Any]
) -> None:
    client.post("/api/datasets", json=canonical_payload)
    response = client.get("/api/datasets")
    assert response.status_code == 200
    names = [d["name"] for d in response.json()["datasets"]]
    assert "canonical" in names


def test_duplicate_dataset_name_is_400(
    client: TestClient, canonical_payload: Dict[str, Any]
) -> None:
    first = client.post("/api/datasets", json=canonical_payload)
    assert first.status_code == 201
    second = client.post("/api/datasets", json=canonical_payload)
    assert second.status_code == 400
    assert second.json()["request_id"]


def test_invalid_corpus_is_rejected_with_request_id(client: TestClient) -> None:
    # Empty transaction list normalizes to an empty corpus -> 400, not 500.
    response = client.post(
        "/api/datasets",
        json={"name": "empty", "transactions": [[]], "min_support": 0.2},
    )
    assert response.status_code == 400
    assert response.json()["request_id"]
