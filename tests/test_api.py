"""End-to-end HTTP tests through the real FastAPI app (in-process ASGI)."""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app


@pytest.fixture
def client(tmp_path):
    log_dir = str(tmp_path / "logs")
    app = create_app(log_dir=log_dir)
    with TestClient(app) as c:
        c._log_dir = log_dir
        yield c


def _post(client, body):
    return client.post("/certify", json=body)


def test_health(client) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_certify_simple_root_full_envelope(client) -> None:
    resp = _post(
        client,
        {"expression": "x^2 - 2", "lower": "1", "upper": "2"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "certified"
    assert len(body["certified_roots"]) == 1
    root = body["certified_roots"][0]
    # Certified vs approximate are separate top-level sections.
    assert "approximate_roots_unverified" in body
    assert root["certification"]["status"] == "certified"
    assert "evidence" in root
    assert body["trace"]["run_id"]


def test_certified_and_approximate_are_separate(client) -> None:
    resp = _post(
        client,
        {
            "expression": "x^2 - 2",
            "lower": "1",
            "upper": "2",
            "include_approximation": True,
        },
    )
    body = resp.json()
    approx = body["approximate_roots_unverified"]
    assert approx["status"] == "approximate_unverified"
    assert "NOT certified" in approx["warning"]
    # Approx value is a float-ish string close to sqrt2.
    assert approx["roots"]
    certified_mid = body["certified_roots"][0]["midpoint"]
    assert approx["roots"][0]["value"] != certified_mid  # distinct fields


def test_parse_error_returns_400_with_position(client) -> None:
    resp = _post(
        client,
        {"expression": "2x", "lower": "0", "upper": "1"},
    )
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "PARSE_ERROR"
    assert err["category"] == "input"
    assert err["position"]["start"] == 0


def test_reversed_interval_returns_409(client) -> None:
    resp = _post(
        client,
        {"expression": "x", "lower": "2", "upper": "1"},
    )
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["category"] == "state_conflict"


def test_schema_violation_returns_400_envelope(client) -> None:
    # Missing required field.
    resp = _post(client, {"expression": "x"})
    assert resp.status_code == 400
    assert resp.json()["error"]["category"] == "input"


def test_domain_error_returns_422(client) -> None:
    resp = _post(
        client,
        {"expression": "sqrt(x-1)", "lower": "0", "upper": "2"},
    )
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "DOMAIN_ERROR"
    assert err["position"]["start"] == 5


def test_resource_exhaustion_is_http_200_with_partial(client) -> None:
    resp = _post(
        client,
        {
            "expression": "sin(x)",
            "lower": "0",
            "upper": "100",
            "max_evals": 40,
            "include_approximation": False,
        },
    )
    # Resource exhaustion yields usable partial results, not an HTTP error.
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["budget_hit"] == "max_evaluations_reached"
    assert body["undecided_regions"]


def test_trace_log_written_and_replayable(client) -> None:
    resp = _post(
        client,
        {"expression": "x^2 - 2", "lower": "1", "upper": "2"},
    )
    run_id = resp.json()["trace"]["run_id"]
    path = os.path.join(client._log_dir, f"run-{run_id}.jsonl")
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as fh:
        lines = [json.loads(line) for line in fh if line.strip()]
    # The on-disk log contains decision reasons needed to replay the run.
    reasons = {entry["reason"] for entry in lines}
    assert "accepted" in reasons
    assert any(
        entry["stage"] == "certify" for entry in lines
    )
    # Each event is sequentially numbered with intermediate state.
    seqs = [entry["seq"] for entry in lines]
    assert seqs == sorted(seqs)


def test_optional_approximation_can_be_disabled(client) -> None:
    resp = _post(
        client,
        {
            "expression": "x^2 - 2",
            "lower": "1",
            "upper": "2",
            "include_approximation": False,
        },
    )
    body = resp.json()
    assert body["approximate_roots_unverified"] is None


def test_sample_count_out_of_range_is_400(client) -> None:
    resp = _post(
        client,
        {
            "expression": "x",
            "lower": "0",
            "upper": "1",
            "sample_count": 2,
        },
    )
    assert resp.status_code == 400


def test_precision_override_takes_effect(client) -> None:
    resp = _post(
        client,
        {
            "expression": "x^2 - 2",
            "lower": "1",
            "upper": "2",
            "precision_dps": 30,
            "target_width": "1e-20",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "certified"
