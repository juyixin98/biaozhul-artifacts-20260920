"""HTTP 服务端到端测试（绑定随机本地端口，不接外部账号）。"""

import json
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import pytest

from dms.server import make_handler


@pytest.fixture()
def server(tmp_path):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                               make_handler(tmp_path / "key.json"))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    host, port = httpd.server_address
    yield f"http://{host}:{port}"
    httpd.shutdown()
    httpd.server_close()
    t.join(timeout=2)


def post(base, path, obj, raw=None):
    data = raw if raw is not None else json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_health(server):
    with urllib.request.urlopen(server + "/health", timeout=5) as r:
        body = json.loads(r.read())
    assert body["ok"] is True


def test_compile_then_mask(server):
    rules_spec = {"version": 1, "rules": [
        {"id": "m", "action": "mask", "path": "$.users[*].phone",
         "options": {"keep_last": 4}}]}
    status, body = post(server, "/v1/compile", rules_spec)
    assert status == 200 and body["ok"]
    compiled = body["compiled"]

    status, body = post(server, "/v1/mask", {
        "rules": rules_spec,
        "document": {"users": [{"phone": "13812345678"}]},
    })
    assert status == 200
    assert body["document"]["users"][0]["phone"] == "*******5678"

    status, body = post(server, "/v1/mask-compiled", {
        "compiled": compiled,
        "document": {"users": [{"phone": "13900009999"}]},
    })
    assert status == 200
    assert body["document"]["users"][0]["phone"] == "*******9999"


def test_unknown_rule_rejected_via_http_and_no_value_echo(server):
    secret = "PLAINTEXT_SECRET_XYZ"
    status, body = post(server, "/v1/mask", {
        "rules": {"version": 1, "rules": [
            {"id": "x", "action": "bogus", "path": "$.a"}]},
        "document": {"a": secret},
    })
    assert status == 422
    assert body["error"]["code"] == "unknown_rule"
    # 错误响应中不得回显原始敏感值
    assert secret not in json.dumps(body, ensure_ascii=False)


def test_rule_conflict_returned_without_leaking_values(server):
    secret = "ANOTHER_SECRET_あ"
    status, body = post(server, "/v1/mask", {
        "rules": {"version": 1, "rules": [
            {"id": "a", "action": "redact", "path": "$.users[*]"},
            {"id": "b", "action": "mask", "path": "$.users[*].x",
             "options": {"keep_last": 0}}]},
        "document": {"users": [{"x": secret}]},
    })
    assert status == 422
    assert body["error"]["code"] == "rule_conflict"
    assert secret not in json.dumps(body, ensure_ascii=False)


def test_invalid_json_body(server):
    status, body = post(server, "/v1/compile", None, raw=b"{not json")
    assert status == 400
    assert body["error"]["code"] == "invalid_payload"


def test_missing_required_field_error(server):
    status, body = post(server, "/v1/mask", {
        "rules": {"version": 1, "rules": [
            {"id": "m", "action": "mask", "path": "$.must_exist",
             "require_match": True, "options": {"keep_last": 0}}]},
        "document": {"other": "x"},
    })
    assert status == 422
    assert body["error"]["code"] == "missing_field"
    assert "must_exist" in json.dumps(body)
