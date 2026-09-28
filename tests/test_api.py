"""FastAPI 接口集成测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from eigenservice.api import app

from .fixtures import mildly_asymmetric_matrix, random_symmetric, with_spectrum

client = TestClient(app)


@pytest.mark.integration
def test_healthz() -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.integration
def test_api_success_shape_and_identity() -> None:
    matrix = with_spectrum([-1.0, 2.0, 4.0], seed=3).tolist()
    resp = client.post(
        "/eigendecompose",
        json={"matrix": matrix, "request_id": "id-abc-123"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["request_id"] == "id-abc-123"
    assert body["size"] == 3
    assert body["eigenvalues"] == pytest.approx([-1.0, 2.0, 4.0], abs=1e-9)
    assert len(body["eigenvectors"]) == 3
    assert body["quality"]["residual"]["relative_fro"] < 1e-9
    assert body["service_version"]
    assert body["trace"]["steps"]


@pytest.mark.integration
def test_api_generated_request_id_when_absent() -> None:
    matrix = random_symmetric(3, seed=5).tolist()
    resp = client.post("/eigendecompose", json={"matrix": matrix})
    assert resp.status_code == 200
    assert resp.json()["request_id"]


@pytest.mark.integration
def test_api_asymmetry_returns_422_with_code() -> None:
    matrix = mildly_asymmetric_matrix(1e-4).tolist()
    resp = client.post("/eigendecompose", json={"matrix": matrix})
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error_code"] == "asymmetric_matrix"
    assert body["details"]["relative_asymmetry"] > 1e-9
    assert body["trace"]["failures"]


@pytest.mark.integration
def test_api_non_square_returns_422() -> None:
    resp = client.post(
        "/eigendecompose", json={"matrix": [[1.0, 2.0], [3.0, 4.0, 5.0]]}
    )
    assert resp.status_code == 422
    # Pydantic 形状在服务层校验
    assert resp.json()["error_code"] == "invalid_matrix"


@pytest.mark.integration
def test_api_zero_budget_returns_409_not_success() -> None:
    matrix = random_symmetric(6, seed=8).tolist()
    resp = client.post(
        "/eigendecompose",
        json={"matrix": matrix, "base_sweeps": 0, "sweep_multiplier": 0},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["success"] is False
    assert body["error_code"] == "not_converged"
    assert body["details"]["max_sweeps"] == 0
    assert body["details"]["stalled_index"] is not None
    # 不确定结论单列
    assert body["trace"]["uncertainties"]
    assert body["trace"]["failures"][0]["code"] == "not_converged"


@pytest.mark.integration
def test_api_config_override_accepted() -> None:
    matrix = mildly_asymmetric_matrix(1e-7).tolist()
    # 默认 sym_tol=1e-9 会拒绝; 放宽后应成功
    resp = client.post(
        "/eigendecompose", json={"matrix": matrix, "sym_tol": 1e-3}
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True
