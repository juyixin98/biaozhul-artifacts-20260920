"""FastAPI 接口契约：成功形状与四类错误的 HTTP 映射可区分。"""

from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from api.main import app
from polyroots.runlog import RunStore
from tests.conftest import poly_from_roots

pytestmark = pytest.mark.integration


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    # 每个测试用独立日志目录，避免污染真实 logs/runs
    store = RunStore(str(tmp_path / "apiruns"))
    monkeypatch.setattr("api.main._store", store)
    return TestClient(app)


def test_health(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_solve_returns_full_error_evidence_contract(client) -> None:
    # x^2 + 1，降序 [1, 0, 1]
    r = client.post("/api/v1/roots", json={
        "coefficients": [1, 0, 1],
        "run_id": "api-x2+1",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == "api-x2+1"
    assert body["status"] == "converged"
    assert body["degree"] == 2
    assert len(body["roots"]) == 2
    for root in body["roots"]:
        assert {"index", "real", "imag", "kind", "relative_residual",
                "sensitivity_indicator", "cluster_separation",
                "cluster_id", "converged", "note"} <= set(root)
    assert body["factor_error"]["max_rel_coeff_error"] < 1e-9
    assert body["vieta"]["sum_rel_error"] < 1e-9
    # 容差必须显式回报
    assert body["tolerances"]["conjugate_tol"] == 1e-8


def test_complex_coefficient_shapes_accepted(client) -> None:
    # (1+i)x + 2：降序 [{real:1,imag:1}, 2]
    r = client.post("/api/v1/roots", json={
        "coefficients": [{"real": 1, "imag": 1}, [2, 0]],
    })
    assert r.status_code == 200
    root = r.json()["roots"][0]
    # 根 = -2/(1+i) = -1+i
    assert root["real"] == pytest.approx(-1.0, abs=1e-10)
    assert root["imag"] == pytest.approx(1.0, abs=1e-10)


def test_zero_polynomial_is_400_input_error(client) -> None:
    r = client.post("/api/v1/roots", json={"coefficients": [0, 0, 0]})
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "zero_polynomial"
    assert body["category"] == "input_error"


def test_non_finite_coefficient_is_distinct_400(client) -> None:
    # httpx 拒绝生成非标准 NaN JSON 令牌，这里直接发送原始 JSON 文本，
    # 验证服务端确实能在数值边界识别并区分 NaN/Inf
    import json
    raw = json.dumps({"coefficients": [1.0, float("nan")]}, allow_nan=True)
    r = client.post("/api/v1/roots", content=raw,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json()["code"] == "non_finite_coefficient"


def test_non_numeric_coefficient_is_400(client) -> None:
    r = client.post("/api/v1/roots", json={"coefficients": [1, "two", 3]})
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_coefficients"


def test_empty_coefficients_rejected_at_schema(client) -> None:
    r = client.post("/api/v1/roots", json={"coefficients": []})
    assert r.status_code == 422  # pydantic min_length


def test_degree_limit_is_413_resource_exhausted(client) -> None:
    coeffs = [-1.0] + [0.0] * 299 + [1.0]  # x^300-1，升序 300 次
    r = client.post("/api/v1/roots", json={
        "coefficients": coeffs, "order": "ascending", "max_degree": 256,
    })
    assert r.status_code == 413
    body = r.json()
    assert body["code"] == "resource_exhausted"
    assert body["category"] == "resource_exhausted"
    assert body["details"]["degree"] == 300


def test_invalid_option_is_400(client) -> None:
    r = client.post("/api/v1/roots", json={
        "coefficients": [1, 0, 1], "convergence_tol": 5.0,
    })
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_option"


def test_computation_failure_is_distinct_422(client, monkeypatch) -> None:
    # 注入内核数值失败，验证“计算失败”与输入/状态/资源错误可区分
    from polyroots.errors import ComputationFailedError
    from polyroots import engine

    def boom(*_args, **_kwargs):
        raise ComputationFailedError(
            "注入的 LAPACK 失败", {"reason": "synthetic-test"}
        )

    monkeypatch.setattr(engine, "run_kernel", boom)
    r = client.post("/api/v1/roots", json={"coefficients": [1, 0, 1]})
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "computation_failed"
    assert body["category"] == "computation_failed"
    # 与其它三类 HTTP 语义不同
    assert r.status_code not in (400, 409, 413)


def test_run_id_conflict_is_409_state_conflict(client) -> None:
    c1 = [[c.real, c.imag] for c in poly_from_roots([1.0, 2.0])[::-1]]
    c2 = [[c.real, c.imag] for c in poly_from_roots([3.0, 4.0])[::-1]]
    r1 = client.post("/api/v1/roots", json={"coefficients": c1, "run_id": "k"})
    assert r1.status_code == 200
    r2 = client.post("/api/v1/roots", json={"coefficients": c2, "run_id": "k"})
    assert r2.status_code == 409
    assert r2.json()["category"] == "state_conflict"


def test_run_retrieval_and_missing_404(client) -> None:
    client.post("/api/v1/roots", json={"coefficients": [1, 0, 1], "run_id": "get1"})
    ok = client.get("/api/v1/runs/get1")
    assert ok.status_code == 200
    assert ok.json()["run_id"] == "get1"
    missing = client.get("/api/v1/runs/nope")
    assert missing.status_code == 404
    assert missing.json()["code"] == "run_not_found"


def test_not_converged_is_still_200_with_explicit_status(client) -> None:
    # 近重根 + 极小迭代预算：迭代耗尽不是 5xx，而是 200 + not_converged
    coeffs = poly_from_roots([1.0, 1.0 + 1e-9, -4.0, 0.5])
    payload = [[c.real, c.imag] for c in coeffs[::-1]]
    r = client.post("/api/v1/roots", json={
        "coefficients": payload,
        "kernel": "aberth",
        "max_iterations": 5,
        "convergence_tol": 1e-13,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "not_converged"
    assert body["kernel"]["unconverged_count"] >= 1
    assert any(root["kind"] == "unconverged" for root in body["roots"])
