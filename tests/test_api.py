"""FastAPI endpoint tests: success envelope and typed failure categories."""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from engine.model import TrainedModel, calibrate_and_freeze
from service.app import create_app
from service.registry import ModelRegistry


@pytest.fixture
def client() -> TestClient:
    rng = np.random.default_rng(99)
    trained = TrainedModel(
        weights=(rng.uniform(-0.3, 0.3, size=(3, 2)),),
        biases=(rng.uniform(-0.1, 0.1, size=(3,)),),
        model_id="api_demo",
    )
    calib = rng.uniform(-2.0, 2.0, size=(128, 2))
    artifact = calibrate_and_freeze(trained, calib)
    registry = ModelRegistry()
    registry.register(artifact, trained)
    return TestClient(create_app(registry=registry))


@pytest.mark.api
def test_health_and_model_listing(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/models").json()["models"] == ["api_demo"]


@pytest.mark.api
def test_infer_success_envelope(client: TestClient) -> None:
    resp = client.post(
        "/models/api_demo/infer", json={"input": [[0.1, -0.2], [0.0, 0.5]]}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["model_id"] == "api_demo"
    assert body["request_id"].startswith("req-")
    assert len(body["output"]) == 2
    assert all(isinstance(v, float) for row in body["output"] for v in row)
    assert body["diagnostics"]["request_id"] == body["request_id"]
    assert body["diagnostics"]["nodes"][0]["op"] == "linear"


@pytest.mark.api
def test_unknown_model_is_reject_404_with_request_id(client: TestClient) -> None:
    resp = client.post("/models/nope/infer", json={"input": [[0.0, 0.0]]})
    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["code"] == "model_not_found"
    assert error["category"] == "REJECT"
    assert error["request_id"].startswith("req-")
    assert "api_demo" in error["details"]["available"]


@pytest.mark.api
def test_malformed_input_is_reject_400(client: TestClient) -> None:
    resp = client.post(
        "/models/api_demo/infer", json={"input": [[0.0]]}  # wrong feature count
    )
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["category"] == "REJECT"
    assert error["code"] == "invalid_input"
    assert error["details"]["expected_features"] == 2
    assert error["details"]["bad_row"] == 0


@pytest.mark.api
def test_non_numeric_input_is_rejected(client: TestClient) -> None:
    resp = client.post(
        "/models/api_demo/infer", json={"input": [["0.1", -0.2]]}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_input"


@pytest.mark.api
def test_out_of_calibration_range_is_reject_422(client: TestClient) -> None:
    resp = client.post(
        "/models/api_demo/infer", json={"input": [[999.0, -999.0]]}
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "input_out_of_calibration_range"
    assert error["category"] == "REJECT"
    # Key state for reproduction: observed vs frozen calibrated range.
    details = error["details"]
    assert details["observed_max"] >= 999.0
    assert "calibrated_min" in details and "calibrated_max" in details
    assert error["request_id"].startswith("req-")


@pytest.mark.api
def test_validate_accepts_in_range_request(client: TestClient) -> None:
    resp = client.post(
        "/models/api_demo/validate",
        json={"input": (np.random.default_rng(0).uniform(-1, 1, size=(16, 2))).tolist()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "ACCEPT"
    report = body["error_report"]
    assert report["within_analytic_bound"] is True
    assert report["bound_violations"] == 0
    assert "analytic_bound_per_channel" in report
    assert body["request_id"] == body["diagnostics"]["request_id"]
