"""API 层测试：用 TestClient 真实走 HTTP 往返，并回放全部示例夹具。"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

client = TestClient(app)


def test_healthz():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "fixture,expected_winner,expected_filtered",
    [
        ("insufficient_resources.json", "node-big", {"node-small"}),
        ("anti_affinity_conflict.json", "node-b", {"node-a"}),
        ("zone_skew.json", "node-b1", set()),
        ("toleration_match.json", "node-normal", {"node-gpu"}),
    ],
)
def test_example_fixtures_over_http(fixture, expected_winner, expected_filtered):
    payload = json.loads((EXAMPLES / fixture).read_text())
    resp = client.post("/v1/analyze", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["winner"] == expected_winner
    filtered = {r["node"] for r in body["results"] if not r["feasible"]}
    assert filtered == expected_filtered
    for r in body["results"]:
        if r["feasible"]:
            assert r["final_score"] is not None and r["rank"] is not None
            assert r["filter_reasons"] == []
        else:
            assert r["filter_reasons"], "被淘汰节点必须给出原因"
            assert r["final_score"] is None and r["rank"] is None


def test_invalid_quantity_rejected_with_422():
    resp = client.post(
        "/v1/analyze",
        json={
            "pod": {"name": "p", "requests": {"cpu": "lots", "memory": "1Gi"}},
            "nodes": [{"name": "n1", "allocatable": {"cpu": "2", "memory": "2Gi"}}],
        },
    )
    assert resp.status_code == 422


def test_empty_nodes_rejected_with_422():
    resp = client.post(
        "/v1/analyze",
        json={"pod": {"name": "p"}, "nodes": []},
    )
    assert resp.status_code == 422
