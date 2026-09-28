"""API 集成测试：具体结果断言 + 失败类别 + 请求标识，不只检查"能调用"。"""

from __future__ import annotations

import json
import math

from fastapi.testclient import TestClient

from app.config import Settings
from app.service import create_app


def _client(**overrides) -> TestClient:
    return TestClient(create_app(Settings(**overrides)))


def _post_lenient(client: TestClient, url: str, payload: dict):
    """httpx 的 json= 拒绝 NaN/Inf；JSON 扩展值用宽松序列化手动发送。"""
    return client.post(
        url,
        content=json.dumps(payload),  # Python json 默认允许 NaN/Infinity
        headers={"content-type": "application/json"},
    )


def test_healthz() -> None:
    resp = _client().get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_policies_endpoint_lists_fixed_rules() -> None:
    body = _client().get("/v1/policies").json()
    for key in ("nan", "mixed_infinities", "signed_zero", "empty_input"):
        assert key in body["policies"]
    assert set(body["methods"]) == {"naive", "pairwise", "compensated"}


def test_sum_cancellation_concrete_results() -> None:
    client = _client()
    # 经典用例 [1e16, 1, -1e16]：朴素 0，补偿 1 —— 断言具体数值
    naive = client.post("/v1/sum", json={"values": [1e16, 1.0, -1e16], "method": "naive"}).json()
    comp = client.post("/v1/sum", json={"values": [1e16, 1.0, -1e16], "method": "compensated"}).json()
    assert naive["result"] == 0.0
    assert comp["result"] == 1.0
    assert comp["error"]["abs_error"] == 0.0
    assert naive["error"]["abs_error"] == 1.0


def test_sum_generator_cancellation() -> None:
    client = _client()
    resp = client.post(
        "/v1/sum",
        json={
            "generator": {"kind": "cancellation", "n_pairs": 100, "magnitude": 1e16, "small": 1.0},
            "method": "compensated",
            "chunk_size": 64,
        },
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["result"] == 100.0
    assert body["diagnostics"]["input_profile"]["count"] == 300
    # 脱敏：画像里没有原始序列
    assert "values" not in body["diagnostics"]["input_profile"]


def test_nan_rejected_with_category_and_request_id() -> None:
    client = _client()
    resp = _post_lenient(client, "/v1/sum", {"values": [1.0, float("nan")], "method": "naive"})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["category"] == "non_finite_input"
    assert err["request_id"]
    assert resp.headers["x-request-id"] == err["request_id"]


def test_empty_input_rejected() -> None:
    resp = _client().post("/v1/sum", json={"values": [], "method": "naive"})
    assert resp.status_code == 422
    assert resp.json()["error"]["category"] == "empty_input"


def test_mixed_infinities_rejected() -> None:
    resp = _post_lenient(_client(), "/v1/sum", {"values": [float("inf"), float("-inf")]})
    assert resp.status_code == 422
    assert resp.json()["error"]["category"] == "mixed_infinities"


def test_single_infinity_returns_inf_repr() -> None:
    resp = _post_lenient(_client(), "/v1/sum", {"values": [1.0, float("inf")]})
    body = resp.json()
    assert resp.status_code == 200
    assert body["result"] is None
    assert body["result_repr"] == "inf"


def test_input_too_large_rejected() -> None:
    client = _client(max_values=3)
    resp = client.post("/v1/sum", json={"values": [1.0, 2.0, 3.0, 4.0]})
    assert resp.status_code == 422
    assert resp.json()["error"]["category"] == "input_too_large"


def test_request_id_header_echoed() -> None:
    client = _client()
    resp = client.post(
        "/v1/sum",
        json={"values": [1.0, 2.0]},
        headers={"X-Request-ID": "trace-abc-123"},
    )
    assert resp.headers["x-request-id"] == "trace-abc-123"
    assert resp.json()["request_id"] == "trace-abc-123"


def test_compare_endpoint_all_methods_and_reorder_probe() -> None:
    client = _client()
    resp = client.post(
        "/v1/compare",
        json={
            "generator": {"kind": "random_spread", "n": 2048, "seed": 5, "lo_exp": -2, "hi_exp": 10},
            "chunk_size": 64,
            "reorder_trials": 6,
            "reorder_seed": 1,
        },
    )
    body = resp.json()
    assert resp.status_code == 200
    assert set(body["methods"]) == {"naive", "pairwise", "compensated"}
    for name, entry in body["methods"].items():
        assert "error" in entry, name
        assert entry["error"]["within_bound"], name
    probe = body["reorder_probe"]
    assert probe["trials"] == 6
    # 朴素法在重排探针下应出现多个不同结果；补偿法散布应远小于朴素法
    assert probe["by_method"]["naive"]["distinct_results"] > 1
    naive_spread = probe["by_method"]["naive"]["max"] - probe["by_method"]["naive"]["min"]
    comp_spread = probe["by_method"]["compensated"]["max"] - probe["by_method"]["compensated"]["min"]
    assert naive_spread > 100 * comp_spread


def test_compare_small_accumulation_compensated_wins() -> None:
    client = _client()
    resp = client.post(
        "/v1/compare",
        json={
            "generator": {"kind": "small_accumulation", "count": 50000, "value": 0.1},
            "chunk_size": 4096,
        },
    )
    body = resp.json()
    naive_err = body["methods"]["naive"]["error"]["abs_error"]
    comp_err = body["methods"]["compensated"]["error"]["abs_error"]
    assert comp_err < naive_err


def test_all_negative_zero_returns_negative_zero_repr() -> None:
    resp = _client().post("/v1/sum", json={"values": [-0.0, -0.0]})
    body = resp.json()
    assert body["result"] == 0.0
    assert math.copysign(1.0, body["result"]) < 0
