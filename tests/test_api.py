"""HTTP service integration tests (FastAPI TestClient, in-process)."""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.api


@pytest.fixture()
def client(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setenv("TWOSLS_DB_PATH", os.path.join(tmpdir, "test-evidence.db"))
    # Re-import settings + module-level store fresh for the isolated DB.
    import importlib

    import twosls.config as config_mod
    importlib.reload(config_mod)
    import twosls.evidence as evidence_mod
    importlib.reload(evidence_mod)
    import twosls.api as api_mod
    importlib.reload(api_mod)
    with TestClient(api_mod.app) as c:
        yield c


def _payload(sample, request_id, **opts):
    from conftest import sample_to_columns, make_spec
    from twosls.contract import EstimationOptions, InstrumentValidityClaim

    return {
        "request_id": request_id,
        "columns": sample_to_columns(sample),
        "spec": make_spec(sample).model_dump(),
        "options": EstimationOptions(**opts).model_dump(),
        "validity_claim": InstrumentValidityClaim(
            exclusion_restriction_asserted=True,
            rationale="synthetic DGP instruments generated independently of e",
        ).model_dump(),
    }


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert body["thresholds"]["weak_f"] == 10.0


def test_estimate_strong_iv_success_shape(client, strong_sample):
    r = client.post("/api/v1/iv/estimate", json=_payload(strong_sample, "api-strong"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["request_id"] == "api-strong"
    names = {c["name"] for c in body["coefficients"]}
    assert names == {"x_end", "w1", "const"}
    # decision summary explains acceptance
    ds = body["decision_summary"]
    assert ds["accepted"] is True
    assert any("rank condition" in line for line in ds["why"])
    # request id echoed as a header too
    assert r.headers["x-request-id"]


def test_estimate_unidentified_returns_typed_envelope(client):
    from experiments.dgp import rank_failure_sample

    r = client.post(
        "/api/v1/iv/estimate", json=_payload(rank_failure_sample(), "api-rankfail")
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "NOT_IDENTIFIED"
    assert err["request_id"] == "api-rankfail"
    assert err["key_state"]["rank"] < err["key_state"]["rank_required"]
    # no observation-level data leaks into the error
    text = r.text
    assert "columns" not in text


def test_strict_weak_instruments_422(client, weak_sample):
    r = client.post(
        "/api/v1/iv/estimate", json=_payload(weak_sample, "api-weak", strict=True)
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "WEAK_INSTRUMENTS"


def test_non_strict_weak_returns_estimate_with_status(client, weak_sample):
    r = client.post("/api/v1/iv/estimate", json=_payload(weak_sample, "api-weak2"))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "weak"
    assert body["warnings"]
    assert body["decision_summary"]["accepted"] is True


def test_validation_error_envelope(client, strong_sample):
    payload = _payload(strong_sample, "api-missing")
    del payload["columns"]["z1"]
    r = client.post("/api/v1/iv/estimate", json=payload)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


def test_overid_rejection_marks_inconclusive(client):
    from experiments.dgp import invalid_instrument_sample

    r = client.post(
        "/api/v1/iv/estimate", json=_payload(invalid_instrument_sample(), "api-invalid")
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "inconclusive"
    assert body["overidentification"]["verdict"] == "reject"
    # Estimate is still returned (HTTP 200) but NOT accepted for structural
    # interpretation: the over-id rejection invalidates the instrument set.
    assert body["decision_summary"]["accepted"] is False
    assert any("over-id" in line for line in body["decision_summary"]["why"])


def test_evidence_roundtrip(client, strong_sample):
    client.post("/api/v1/iv/estimate", json=_payload(strong_sample, "api-persist"))
    r = client.get("/api/v1/runs/api-persist")
    assert r.status_code == 200
    run = r.json()
    assert run["status"] == "ok"
    assert run["exclusion_asserted"] == 1
    assert run["key_state"]["rank_condition"] is True
    assert len(run["coefficients"]) == 3
    # rejected requests are persisted too
    from experiments.dgp import rank_failure_sample

    client.post("/api/v1/iv/estimate", json=_payload(rank_failure_sample(), "api-persist-fail"))
    r2 = client.get("/api/v1/runs/api-persist-fail")
    assert r2.json()["status"] == "rejected"
    assert r2.json()["error_code"] == "NOT_IDENTIFIED"


def test_run_not_found(client):
    r = client.get("/api/v1/runs/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_list_runs(client, strong_sample, weak_sample):
    client.post("/api/v1/iv/estimate", json=_payload(strong_sample, "list-1"))
    client.post("/api/v1/iv/estimate", json=_payload(weak_sample, "list-2"))
    r = client.get("/api/v1/runs")
    ids = {row["request_id"] for row in r.json()["runs"]}
    assert {"list-1", "list-2"} <= ids


def test_robust_covariance_option(client, strong_sample):
    r = client.post(
        "/api/v1/iv/estimate", json=_payload(strong_sample, "api-robust", covariance="robust")
    )
    assert r.status_code == 200
    assert r.json()["covariance_kind"] == "robust"
