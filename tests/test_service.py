"""HTTP 服务测试:真实起服务、发请求、校验响应。"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from calibration_eval.service import create_server


@pytest.fixture()
def server_url():
    server = create_server(port=0)  # 端口 0:由系统分配空闲端口
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _post(url: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_health(server_url):
    with urllib.request.urlopen(f"{server_url}/health", timeout=10) as resp:
        assert resp.status == 200
        assert json.loads(resp.read()) == {"status": "ok"}


def test_evaluate_ok(server_url):
    status, body = _post(
        f"{server_url}/evaluate",
        {"y_true": [1, 0, 1, 0], "y_prob": [0.9, 0.2, 0.6, 0.4], "n_bins": 2},
    )
    assert status == 200
    assert body["brier_score"] == pytest.approx(0.0925)
    assert body["ece"] == pytest.approx(0.275)
    assert body["n_samples"] == 4


def test_evaluate_with_weights(server_url):
    status, body = _post(
        f"{server_url}/evaluate",
        {
            "y_true": [0, 1],
            "y_prob": [0.3, 0.7],
            "sample_weight": [3, 2],
            "n_bins": 2,
        },
    )
    assert status == 200
    assert body["total_weight"] == pytest.approx(5.0)


def test_evaluate_invalid_probability_returns_400(server_url):
    status, body = _post(
        f"{server_url}/evaluate", {"y_true": [0, 1], "y_prob": [-0.1, 0.5]}
    )
    assert status == 400
    assert "y_prob" in body["error"]


def test_evaluate_missing_fields_returns_400(server_url):
    status, body = _post(f"{server_url}/evaluate", {"y_true": [0, 1]})
    assert status == 400
    assert "y_prob" in body["error"]


def test_evaluate_bad_json_returns_400(server_url):
    req = urllib.request.Request(
        f"{server_url}/evaluate",
        data=b"not-json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=10)
    assert exc_info.value.code == 400


def test_unknown_path_returns_404(server_url):
    status, _ = _post(f"{server_url}/nope", {"y_true": [0], "y_prob": [0.5]})
    assert status == 404
