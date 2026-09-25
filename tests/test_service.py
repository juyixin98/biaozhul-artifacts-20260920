"""HTTP 服务端到端测试：真实端口、真实 JSON 请求（仅标准库客户端）。"""
from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from drift import synthetic
from drift.service import BaselineStore, create_handler


@pytest.fixture(scope="module")
def server_url() -> str:
    store = BaselineStore()
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), create_handler(store)
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    yield f"http://{host}:{port}"
    httpd.shutdown()
    httpd.server_close()


def _request(url: str, method: str, path: str, payload=None, raw: bytes | None = None):
    if raw is not None:
        data = raw
    elif payload is not None:
        data = json.dumps(payload).encode("utf-8")
    else:
        data = None
    req = Request(
        url + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_healthz(server_url: str) -> None:
    status, body = _request(server_url, "GET", "/healthz")
    assert status == 200
    assert body["status"] == "ok"


def test_monitor_same_distribution_low_psi(server_url: str) -> None:
    b, c = synthetic.same_distribution()
    status, body = _request(
        server_url, "POST", "/v1/monitor",
        {"baseline": {"x": b.tolist()}, "current": {"x": c.tolist()}},
    )
    assert status == 200
    result = body["results"]["x"]
    assert result["psi"] < 0.1
    assert result["psi_band"] == "little_drift_rule_of_thumb"
    assert "经验规则" in body["disclaimer"]


def test_monitor_shifted_distribution_high_psi(server_url: str) -> None:
    b, c = synthetic.shifted_distribution(mean_shift=1.0)
    status, body = _request(
        server_url, "POST", "/v1/monitor",
        {"baseline": {"x": b.tolist()}, "current": {"x": c.tolist()}},
    )
    assert status == 200
    assert body["results"]["x"]["psi"] > 0.25


def test_baseline_then_drift_flow(server_url: str) -> None:
    b, c = synthetic.shifted_distribution(mean_shift=1.5)
    status, created = _request(
        server_url, "POST", "/v1/baselines",
        {"features": {"x": b.tolist()}, "n_bins": 10},
    )
    assert status == 201
    baseline_id = created["baseline_id"]
    assert "x" in created["features"]
    assert len(created["features"]["x"]["baseline_counts"]) == 13

    status, summary = _request(
        server_url, "GET", f"/v1/baselines/{baseline_id}"
    )
    assert status == 200
    assert summary["baseline_id"] == baseline_id

    status, body = _request(
        server_url, "POST", "/v1/drift",
        {"baseline_id": baseline_id, "current": {"x": c.tolist()}},
    )
    assert status == 200
    assert body["results"]["x"]["psi"] > 0.25


def test_drift_unknown_baseline_is_404(server_url: str) -> None:
    status, body = _request(
        server_url, "POST", "/v1/drift",
        {"baseline_id": "nope", "current": {"x": [1.0, 2.0]}},
    )
    assert status == 404
    assert "不存在" in body["error"]


def test_drift_unknown_feature_is_400(server_url: str) -> None:
    b, _ = synthetic.same_distribution()
    _, created = _request(
        server_url, "POST", "/v1/baselines",
        {"features": {"x": b.tolist()}},
    )
    status, body = _request(
        server_url, "POST", "/v1/drift",
        {
            "baseline_id": created["baseline_id"],
            "current": {"x": [0.1, 0.2], "ghost": [1.0]},
        },
    )
    assert status == 400
    assert "ghost" in body["error"]


def test_monitor_feature_mismatch_is_400(server_url: str) -> None:
    status, body = _request(
        server_url, "POST", "/v1/monitor",
        {"baseline": {"a": [1.0, 2.0]}, "current": {"b": [1.0, 2.0]}},
    )
    assert status == 400


def test_baseline_all_missing_feature_is_422(server_url: str) -> None:
    status, body = _request(
        server_url, "POST", "/v1/baselines",
        {"features": {"x": [None, None]}},
    )
    assert status == 422
    assert "无法建档" in body["error"]


def test_invalid_json_body_is_400(server_url: str) -> None:
    status, body = _request(
        server_url, "POST", "/v1/monitor", raw=b"{not json"
    )
    assert status == 400
    assert "JSON" in body["error"]


def test_illegal_elements_are_400(server_url: str) -> None:
    status, body = _request(
        server_url, "POST", "/v1/monitor",
        {
            "baseline": {"x": [1.0, 2.0, 3.0]},
            "current": {"x": [1.0, True, 3.0]},
        },
    )
    assert status == 400


def test_invalid_smoothing_is_400(server_url: str) -> None:
    status, body = _request(
        server_url, "POST", "/v1/demo", {"scenario": "same", "smoothing": "x"}
    )
    assert status == 400


def test_demo_scenarios(server_url: str) -> None:
    for scenario in ("same", "shifted", "scaled", "extremes"):
        status, body = _request(
            server_url, "POST", "/v1/demo", {"scenario": scenario}
        )
        assert status == 200
        assert "result" in body and "psi" in body["result"]

    status, body = _request(
        server_url, "POST", "/v1/demo", {"scenario": "all_missing"}
    )
    assert status == 200
    assert body["result"]["current_all_missing"] is True
    assert body["result"]["n_current_observed"] == 0

    status, body = _request(
        server_url, "POST", "/v1/demo", {"scenario": "small_sample"}
    )
    assert status == 200
    assert body["result"]["small_sample"] is True


def test_demo_unknown_scenario_is_400(server_url: str) -> None:
    status, _ = _request(
        server_url, "POST", "/v1/demo", {"scenario": "bogus"}
    )
    assert status == 400


def test_unknown_path_is_404(server_url: str) -> None:
    status, _ = _request(server_url, "GET", "/nope")
    assert status == 404


def test_null_values_count_as_missing(server_url: str) -> None:
    b, _ = synthetic.same_distribution()
    status, body = _request(
        server_url, "POST", "/v1/monitor",
        {
            "baseline": {"x": b.tolist()},
            "current": {"x": [None, 1.0, None, 0.5]},
        },
    )
    assert status == 200
    result = body["results"]["x"]
    assert result["n_current"] == 4
    assert result["n_current_observed"] == 2
    assert result["missing_rate_current"] == 0.5
