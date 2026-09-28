"""Service contract tests: HTTP statuses keep failure categories
distinguishable, and every run is persisted to the JSONL run log."""

import json

import pytest
from fastapi.testclient import TestClient
from mpmath import mp, mpf

from interval_cert import service
from interval_cert.service import app

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def run_log(tmp_path, monkeypatch):
    log_file = tmp_path / "runs.jsonl"
    monkeypatch.setattr(service, "LOG_DIR", tmp_path)
    monkeypatch.setattr(service, "RUN_LOG", log_file)
    return log_file


def _certify_payload(**overrides):
    payload = {
        "expression": "x^2 - 2",
        "variable": "x",
        "interval": {"lo": 0.0, "hi": 2.0},
        "options": {"tol": 1e-12},
    }
    payload.update(overrides)
    return payload


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_certify_simple_root_happy_path(run_log):
    response = client.post("/certify", json=_certify_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["error"] is None

    assert len(body["certified_roots"]) == 1
    root = body["certified_roots"][0]
    with mp.workdps(80):
        sqrt2 = mp.sqrt(2)
        assert mpf(root["interval"]["lo"]) <= sqrt2 <= mpf(root["interval"]["hi"])

    evidence = root["evidence"]
    assert evidence["theorem"] == "interval_newton_contraction"
    assert evidence["witnesses"], "certified root must carry a theorem witness"

    # Approximations are present but strictly separated and uncertified.
    assert body["approximations"]
    assert all(a["certified"] is False for a in body["approximations"])

    # Trace is replayable: run id, step, action, reason on every record.
    assert body["trace"]
    assert all(r["run_id"] == body["run_id"] for r in body["trace"])

    # The run was persisted for offline replay.
    lines = run_log.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["run_id"] == body["run_id"]
    assert record["status"] == "completed"
    assert record["request"]["expression"] == "x^2 - 2"


def test_invalid_expression_is_input_error(run_log):
    response = client.post("/certify", json=_certify_payload(expression="tan(x)"))
    assert response.status_code == 400
    assert response.json()["error"]["category"] == "input_error"


def test_malformed_expression_is_input_error(run_log):
    response = client.post("/certify", json=_certify_payload(expression="x +"))
    assert response.status_code == 400
    assert response.json()["error"]["category"] == "input_error"


def test_inverted_interval_is_input_error(run_log):
    response = client.post(
        "/certify", json=_certify_payload(interval={"lo": 2.0, "hi": 1.0}))
    assert response.status_code == 400
    assert response.json()["error"]["category"] == "input_error"


def test_tolerance_precision_mismatch_is_state_conflict(run_log):
    response = client.post(
        "/certify",
        json=_certify_payload(options={"tol": 1e-100, "dps": 30}),
    )
    assert response.status_code == 409
    assert response.json()["error"]["category"] == "state_conflict"


def test_domain_error_carries_location(run_log):
    response = client.post(
        "/certify",
        json=_certify_payload(expression="log(x)", interval={"lo": -1.0, "hi": 1.0}),
    )
    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "domain_error"
    assert body["error"]["category"] == "domain_error"
    assert body["error"]["details"]["location"] == "root"


def test_resource_exhaustion_is_partial_success(run_log):
    response = client.post(
        "/certify",
        json=_certify_payload(
            expression="sin(x)",
            interval={"lo": -100.0, "hi": 100.0},
            options={"max_steps": 5},
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "resource_exhausted"
    assert body["undecided"], "exhausted runs must report leftover boxes"
    assert all(u["reason"] == "resource_exhausted" for u in body["undecided"])


def test_double_root_returns_undecided_not_certified(run_log):
    response = client.post(
        "/certify",
        json=_certify_payload(expression="(x-1)^2", interval={"lo": 0.0, "hi": 2.0}),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["certified_roots"] == []
    assert body["undecided"], "a repeated root must remain undecided"


def test_persisted_log_keeps_trace_when_response_trims_it(run_log):
    response = client.post(
        "/certify", json=_certify_payload(options={"include_trace": False}))
    assert response.status_code == 200
    assert response.json()["trace"] == []  # response payload trimmed
    record = json.loads(run_log.read_text().strip().splitlines()[-1])
    assert record["trace"], "persisted run log must keep the full trace"


def test_pydantic_rejects_nonpositive_tolerance(run_log):
    response = client.post(
        "/certify", json=_certify_payload(options={"tol": 0.0}))
    assert response.status_code == 422  # request-schema validation
    assert "detail" in response.json()
