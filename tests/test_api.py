"""End-to-end API behavior via FastAPI TestClient."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture_body(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _create_corpus(client, name: str) -> int:
    body = _fixture_body(name)
    resp = client.post("/v1/corpora", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["corpus_id"]


def test_full_pipeline_basic(client):
    corpus_id = _create_corpus(client, "basic")

    mine = client.post(f"/v1/corpora/{corpus_id}/mine", json={"min_support": 0.6})
    assert mine.status_code == 200
    assert mine.json()["n_itemsets"] == 8

    rules = client.post(
        f"/v1/corpora/{corpus_id}/rules", json={"min_confidence": 0.8}
    )
    assert rules.status_code == 200
    payload = rules.json()
    assert payload["n_rules"] == 1
    rule = payload["rules"][0]
    assert rule["antecedent"] == ["beer"]
    assert rule["consequent"] == ["diapers"]
    assert rule["confidence"] == pytest.approx(1.0)
    assert rule["lift"] == pytest.approx(1.25)
    assert rule["leverage"] == pytest.approx(0.12)
    assert "SMALL_SAMPLE" in rule["warnings"]


def test_request_id_echoed_and_generated(client):
    resp = client.get("/health", headers={"X-Request-ID": "audit-req-1"})
    assert resp.headers["X-Request-ID"] == "audit-req-1"
    corpus_id = _create_corpus(client, "basic")
    resp = client.post(f"/v1/corpora/{corpus_id}/mine", json={"min_support": 0.6})
    assert resp.json()["request_id"]
    assert resp.headers["X-Request-ID"] == resp.json()["request_id"]


def test_evaluate_zero_denominator_is_undefined_not_zero(client):
    corpus_id = _create_corpus(client, "basic")
    resp = client.post(
        f"/v1/corpora/{corpus_id}/rules/evaluate",
        json={"antecedent": ["caviar"], "consequent": ["beer"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["confidence"] is None  # undefined: count(antecedent) == 0
    assert body["lift"] is None
    assert body["support"] == 0.0
    assert body["leverage"] == pytest.approx(0.0)


@pytest.mark.parametrize(
    "payload, category",
    [
        ({"antecedent": [], "consequent": ["beer"]}, "EMPTY_ANTECEDENT"),
        ({"antecedent": ["beer"], "consequent": []}, "EMPTY_CONSEQUENT"),
        ({"antecedent": ["beer"], "consequent": ["beer"]}, "OVERLAPPING_SIDES"),
    ],
)
def test_evaluate_scope_rejections(client, payload, category):
    corpus_id = _create_corpus(client, "basic")
    resp = client.post(f"/v1/corpora/{corpus_id}/rules/evaluate", json=payload)
    assert resp.status_code == 422
    assert resp.json()["category"] == category
    assert resp.json()["request_id"]


def test_unknown_corpus_rejected(client):
    resp = client.post("/v1/corpora/999/mine", json={"min_support": 0.5})
    assert resp.status_code == 404
    assert resp.json()["category"] == "CORPUS_NOT_FOUND"


def test_rules_before_mining_rejected(client):
    corpus_id = _create_corpus(client, "basic")
    resp = client.post(f"/v1/corpora/{corpus_id}/rules", json={"min_confidence": 0.5})
    assert resp.status_code == 422
    assert resp.json()["category"] == "ITEMSETS_NOT_MINED"


def test_invalid_corpus_payload_rejected(client):
    resp = client.post(
        "/v1/corpora",
        json={"name": "bad", "transactions": [{"transaction_id": "T1", "items": []}]},
    )
    assert resp.status_code == 422
    assert resp.json()["category"] == "EMPTY_TRANSACTION"


def test_threshold_validation_at_api(client):
    corpus_id = _create_corpus(client, "basic")
    resp = client.post(f"/v1/corpora/{corpus_id}/mine", json={"min_support": 0.0})
    assert resp.status_code == 422
