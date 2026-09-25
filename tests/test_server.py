import json
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import pytest

from abseq.server import Handler


@pytest.fixture()
def server_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _post(url, path, payload):
    request = urllib.request.Request(
        url + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class TestServer:
    def test_health(self, server_url):
        with urllib.request.urlopen(server_url + "/health") as response:
            body = json.loads(response.read())
        assert body == {"ok": True, "data": {"status": "up"}, "error": None}

    def test_analyze_endpoint(self, server_url):
        status, body = _post(
            server_url,
            "/v1/analyze",
            {"control": [1.0, 2.0, 3.0], "treatment": [2.0, 3.0, 4.0]},
        )
        assert status == 200
        assert body["ok"] is True
        assert body["data"]["mean_difference"] == pytest.approx(1.0)
        assert "validity_warning" in body["data"]

    def test_analyze_groups_with_missing(self, server_url):
        status, body = _post(
            server_url,
            "/v1/analyze",
            {
                "groups": ["control", "control", "control",
                           "treatment", "treatment", "treatment"],
                "observations": [1.0, None, 1.2, 2.0, 3.0, 4.0],
                "missing_strategy": "drop",
            },
        )
        assert status == 200
        assert body["data"]["n_missing_control"] == 1

    def test_analyze_rejects_bad_strategy(self, server_url):
        status, body = _post(
            server_url,
            "/v1/analyze",
            {"control": [1.0, 2.0], "treatment": [1.0, 2.0],
             "missing_strategy": "bogus"},
        )
        assert status == 400
        assert body["ok"] is False
        assert "missing_strategy" in body["error"]

    def test_analyze_rejects_missing_field(self, server_url):
        status, body = _post(server_url, "/v1/analyze", {"control": [1.0, 2.0]})
        assert status == 400
        assert "treatment" in body["error"]

    def test_coverage_endpoint(self, server_url):
        status, body = _post(
            server_url,
            "/v1/coverage",
            {"n_control": 50, "n_treatment": 50, "reps": 100, "seed": 0},
        )
        assert status == 200
        assert 0.0 <= body["data"]["empirical_coverage"] <= 1.0

    def test_peeking_endpoint(self, server_url):
        status, body = _post(
            server_url,
            "/v1/peeking",
            {"n_per_group": 100, "looks": 4, "reps": 100, "seed": 0},
        )
        assert status == 200
        assert 0.0 <= body["data"]["ever_significant_rate"] <= 1.0

    def test_unknown_route_404(self, server_url):
        status, body = _post(server_url, "/v1/nope", {})
        assert status == 404
        assert body["ok"] is False

    def test_non_object_body_400(self, server_url):
        request = urllib.request.Request(
            server_url + "/v1/analyze",
            data=b"[1, 2, 3]",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request)
        assert excinfo.value.code == 400
