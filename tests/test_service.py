"""HTTP 服务的端到端测试（127.0.0.1 随机端口，无外部网络）。"""
from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

import pitjoin.service
from pitjoin.service import _Handler


@pytest.fixture()
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    yield host, port
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)


def _post(port, path, payload):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    body = json.dumps(payload).encode("utf-8")
    conn.request("POST", path, body=body,
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = json.loads(resp.read().decode("utf-8"))
    conn.close()
    return resp.status, data


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode("utf-8"))
    conn.close()
    return resp.status, data


class TestHealthAndRouting:
    def test_health(self, server):
        # Act
        status, data = _get(server[1], "/health")
        # Assert
        assert status == 200
        assert data["status"] == "ok"

    def test_unknown_route_404(self, server):
        status, _ = _get(server[1], "/nope")
        assert status == 404


class TestExplainEndpoint:
    def test_explain_late_revision_on_canonical(self, server):
        # Arrange：E2 时刻 rA3 尚未入库
        payload = {
            "dataset": "canonical",
            "entity_id": "cust_A",
            "feature_name": "f_balance",
            "event_time": "2026-01-02T12:00:00Z",
        }
        # Act
        status, data = _post(server[1], "/explain", payload)
        # Assert
        assert status == 200
        assert data["reason"] == "SELECTED"
        assert data["selected"]["record_id"] == "rA2"
        causes = [e["cause"] for e in data["excluded"]]
        assert "LATE_INGEST" in causes
        assert any(e["record_id"] == "rA3" for e in data["excluded"])

    def test_explain_version_tie_break(self, server):
        payload = {
            "dataset": "canonical",
            "entity_id": "cust_B",
            "feature_name": "f_score",
            "event_time": "2026-01-02T00:00:00Z",
        }
        status, data = _post(server[1], "/explain", payload)
        assert status == 200
        assert data["selected"]["version"] == 3

    def test_explain_missing_key(self, server):
        payload = {
            "dataset": "canonical",
            "entity_id": "cust_E",
            "feature_name": "f_balance",
            "event_time": "2026-01-02T00:00:00Z",
        }
        status, data = _post(server[1], "/explain", payload)
        assert status == 200
        assert data["reason"] == "MISSING_KEY"


class TestJoinEndpoint:
    def test_join_with_custom_records(self, server):
        # Arrange：自带记录，演示晚到修订
        payload = {
            "records": [
                {"entity_id": "u1", "feature_name": "f", "value": 1.0,
                 "effective_time": "2026-01-01T00:00:00Z",
                 "ingest_time": "2026-01-01T00:00:00Z", "record_id": "v1"},
                {"entity_id": "u1", "feature_name": "f", "value": 2.0,
                 "effective_time": "2026-01-01T00:00:00Z",
                 "ingest_time": "2026-01-05T00:00:00Z", "version": 2,
                 "record_id": "v2-late"},
            ],
            "events": [
                {"entity_id": "u1", "event_time": "2026-01-02T00:00:00Z"},
                {"entity_id": "u1", "event_time": "2026-01-06T00:00:00Z"},
            ],
        }
        # Act
        status, data = _post(server[1], "/join", payload)
        # Assert
        assert status == 200
        assert data["rows"][0]["features"]["f"] == 1.0
        assert data["rows"][1]["features"]["f"] == 2.0

    def test_join_naive_mode_leaks(self, server):
        payload = {
            "dataset": "canonical",
            "use_event_as_of": False,
            "events": [
                {"entity_id": "cust_A", "event_time": "2026-01-02T12:00:00Z"},
            ],
            "features": ["f_balance"],
        }
        status, data = _post(server[1], "/join", payload)
        assert status == 200
        assert data["rows"][0]["features"]["f_balance"] == 111.0  # 泄漏 rA3


class TestInputValidation:
    def test_missing_field_is_400(self, server):
        status, data = _post(server[1], "/explain",
                             {"entity_id": "cust_A"})
        assert status == 400
        assert "缺少字段" in data["error"]

    def test_bad_json_is_400(self, server):
        conn = http.client.HTTPConnection("127.0.0.1", server[1], timeout=5)
        conn.request("POST", "/explain", body=b"{not-json",
                     headers={"Content-Type": "application/json",
                              "Content-Length": "9"})
        resp = conn.getresponse()
        assert resp.status == 400
        conn.close()

    def test_bad_timestamp_is_400(self, server):
        payload = {
            "records": [
                {"entity_id": "u1", "feature_name": "f", "value": 1.0,
                 "effective_time": "not-a-time",
                 "ingest_time": "2026-01-01T00:00:00Z"},
            ],
            "events": [
                {"entity_id": "u1", "event_time": "2026-01-02T00:00:00Z"},
            ],
        }
        status, data = _post(server[1], "/join", payload)
        assert status == 400

    def test_empty_events_is_400(self, server):
        status, data = _post(server[1], "/join", {"events": []})
        assert status == 400


class TestAdditionalValidation:
    def test_generated_dataset_join(self, server):
        payload = {
            "dataset": "generated",
            "events": [
                {"entity_id": "ent_000", "event_time": "2026-01-02T12:00:00Z"},
            ],
        }
        status, data = _post(server[1], "/join", payload)
        assert status == 200
        assert data["feature_names"] == ["f_risk"]
        assert data["rows"][0]["features"]["f_risk"] is not None

    def test_unknown_dataset_is_400(self, server):
        payload = {
            "dataset": "nope",
            "events": [
                {"entity_id": "u", "event_time": "2026-01-02T00:00:00Z"},
            ],
        }
        status, data = _post(server[1], "/join", payload)
        assert status == 400
        assert "未知 dataset" in data["error"]

    def test_records_must_be_nonempty_array(self, server):
        payload = {"records": [], "events": [
            {"entity_id": "u", "event_time": "2026-01-02T00:00:00Z"}]}
        status, data = _post(server[1], "/join", payload)
        assert status == 400

    def test_record_must_be_object(self, server):
        payload = {"records": [42], "events": [
            {"entity_id": "u", "event_time": "2026-01-02T00:00:00Z"}]}
        status, data = _post(server[1], "/join", payload)
        assert status == 400
        assert "对象" in data["error"]

    def test_record_missing_field(self, server):
        payload = {"records": [{"entity_id": "u"}], "events": [
            {"entity_id": "u", "event_time": "2026-01-02T00:00:00Z"}]}
        status, data = _post(server[1], "/join", payload)
        assert status == 400
        assert "缺少字段" in data["error"]

    def test_record_non_string_id(self, server):
        payload = {"records": [{
            "entity_id": 1, "feature_name": "f", "value": 1.0,
            "effective_time": "2026-01-01T00:00:00Z",
            "ingest_time": "2026-01-01T00:00:00Z"}], "events": [
            {"entity_id": "1", "event_time": "2026-01-02T00:00:00Z"}]}
        status, data = _post(server[1], "/join", payload)
        assert status == 400

    def test_event_element_must_be_object(self, server):
        payload = {"events": [5]}
        status, data = _post(server[1], "/join", payload)
        assert status == 400

    def test_features_must_be_string_array(self, server):
        payload = {"dataset": "canonical", "features": [1, 2], "events": [
            {"entity_id": "cust_A", "event_time": "2026-01-02T00:00:00Z"}]}
        status, data = _post(server[1], "/join", payload)
        assert status == 400

    def test_unknown_post_route_404(self, server):
        status, _ = _post(server[1], "/nope", {})
        assert status == 404

    def test_non_object_json_body_is_400(self, server):
        conn = http.client.HTTPConnection("127.0.0.1", server[1], timeout=5)
        conn.request("POST", "/explain", body=b"[1,2,3]",
                     headers={"Content-Type": "application/json",
                              "Content-Length": "7"})
        assert conn.getresponse().status == 400
        conn.close()

    def test_oversized_body_is_400(self, server):
        big = json.dumps({"dataset": "canonical", "entity_id": "x",
                          "feature_name": "f", "event_time": "t",
                          "pad": "a" * (1 << 20)})
        status, _ = _post(server[1], "/explain", json.loads(big))
        assert status == 400


def test_serve_handles_keyboard_interrupt(monkeypatch):
    class _FakeServer:
        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    monkeypatch.setattr("pitjoin.service.create_server",
                        lambda host, port: _FakeServer())
    pitjoin.service.serve()  # 不应抛出


def test_handle_functions_directly():
    # 不经过网络，直接调用处理函数
    from pitjoin.service import handle_explain

    out = handle_explain({
        "dataset": "canonical",
        "entity_id": "cust_A",
        "feature_name": "f_balance",
        "event_time": "2026-01-02T12:00:00Z",
    })
    assert out["selected"]["record_id"] == "rA2"
