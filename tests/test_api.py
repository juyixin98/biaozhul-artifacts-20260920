"""FastAPI integration tests: concrete status codes, codes and payloads."""

import numpy as np
import pytest

from ipw_ate.api import app, reset_store
from ipw_ate.synthetic import (
    make_no_overlap_data,
    make_overlap_data,
    make_propensity_misspecified_data,
)


@pytest.fixture()
def client(tmp_path):
    reset_store(str(tmp_path / "test_runs.db"))
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c
    reset_store(None)


def _payload(data, **over):
    body = {
        "treatment": data.treatment.tolist(),
        "outcome": data.outcome.tolist(),
        "covariates": data.covariates.tolist(),
        "n_splits": 5,
    }
    body.update(over)
    return body


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_analyze_good_data_returns_estimate_and_accept(client):
    data = make_overlap_data(1000, seed=101)
    r = client.post("/analyze", json=_payload(data, request_id="api-ok"))
    assert r.status_code == 200
    body = r.json()
    assert body["request_id"] == "api-ok"
    assert body["diagnostic"]["decision"] == "accept"
    assert abs(body["estimate"] - 2.0) < 0.4
    assert body["ci"]["lower"] < body["estimate"] < body["ci"]["upper"]
    assert "max_balance_z" in body["diagnostic"]
    assert body["diagnostic"]["max_balance_z"] < 4.0
    assert "caveat" in body and "unconfoundedness" in body["caveat"]


def test_analyze_no_overlap_is_409_with_cell_reason(client):
    data = make_no_overlap_data(800, seed=44)
    r = client.post("/analyze", json=_payload(data, n_splits=4,
                                              request_id="api-noov"))
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "overlap_violation_error"
    assert err["request_id"] == "api-noov"
    assert err["diagnostic"]["decision"] == "reject"
    assert "no_overlap_cell" in err["diagnostic"]["reasons"]


def test_analyze_non_binary_treatment_is_422_classified(client):
    data = make_overlap_data(200, seed=5)
    payload = _payload(data)
    bad = payload["treatment"]
    bad[0] = 7
    r = client.post("/analyze", json=payload)
    assert r.status_code == 422
    # Either pydantic list-int typing or kernel validation; both must surface
    # a classified error envelope.
    body = r.json()
    assert "error" in body or "detail" in body


def test_analyze_shape_mismatch_is_422(client):
    body = {
        "treatment": [0, 1, 1, 0, 1, 0],
        "outcome": [0.0, 1.0, 2.0],  # mismatched length
        "covariates": [[0.0], [1.0], [1.0], [0.0], [1.0], [0.0]],
    }
    r = client.post("/analyze", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "data_validation_error"


def test_analyze_propensity_misspec_is_409(client):
    data = make_propensity_misspecified_data(1500, seed=71)
    r = client.post("/analyze", json=_payload(data, request_id="api-balmiss"))
    assert r.status_code == 409
    d = r.json()["error"]["diagnostic"]
    assert "covariate_imbalance" in d["reasons"]
    assert d["max_balance_z"] > 4.0


def test_run_record_persisted_and_fetchable(client):
    data = make_overlap_data(800, seed=101)
    client.post("/analyze", json=_payload(data, request_id="persist-1"))
    r = client.get("/runs/persist-1")
    assert r.status_code == 200
    row = r.json()
    assert row["request_id"] == "persist-1"
    assert row["decision"] == "accept"
    assert row["max_balance_z"] is not None
    # Persisted row contains aggregates, never raw rows.
    assert "covariates" not in row and "outcome" not in row


def test_missing_run_is_404(client):
    r = client.get("/runs/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
