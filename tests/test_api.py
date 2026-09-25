"""HTTP API 端到端测试（真实线程 + 127.0.0.1 套接字）。"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from mde.api import build_server
from mde.crypto import generate_signing_key
from mde.policy import InMemoryPolicyStore
from mde.service import ExportService


@pytest.fixture
def server():
    store = InMemoryPolicyStore()
    svc = ExportService(store, signing_private_key=generate_signing_key())
    srv = build_server(store, svc, host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    host, port = srv.server_address
    base = f"http://{host}:{port}"
    try:
        yield base
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _req(base, method, path, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


POLICY_DOC = {
    "rules": [
        {"path": "name", "action": "allow"},
        {"path": "email", "action": "generalize", "generalizer": "email_mask"},
        {"path": "ssn", "action": "deny"},
    ],
    "description": "hr export",
}


def test_health(server):
    code, body = _req(server, "GET", "/health")
    assert code == 200 and body["status"] == "ok"
    assert len(bytes.fromhex(body["verifying_key_hex"])) == 32


def test_publish_export_verify_flow(server):
    code, p = _req(server, "POST", "/policies/hr/publish", POLICY_DOC)
    assert code == 201 and p["version"] == 1

    data = {"name": "A", "email": "a@x.com", "ssn": "1", "mystery": 2}
    code, body = _req(server, "POST", "/v1/exports",
                      {"data": data, "policy_id": "hr", "purpose": "analytics"})
    assert code == 201
    bundle = body["bundle"]
    assert bundle["output"]["name"] == "A"
    assert bundle["output"]["email"] == "a***@x.com"
    assert "ssn" not in bundle["output"]
    assert "mystery" not in bundle["output"]

    code, report = _req(server, "POST", "/v1/exports/verify",
                        {"bundle": bundle, "source_data": data})
    assert code == 200 and report["ok"]


def test_policy_conflict_returns_409(server):
    _req(server, "POST", "/policies/hr/publish", POLICY_DOC)  # v1
    code, _ = _req(server, "POST",
                   "/policies/hr/publish?expected_version=1", POLICY_DOC)
    assert code == 201  # v2
    code, err = _req(server, "POST",
                     "/policies/hr/publish?expected_version=1", POLICY_DOC)
    assert code == 409
    assert err["current_version"] == 2


def test_pin_specific_version_via_api(server):
    _req(server, "POST", "/policies/hr/publish", POLICY_DOC)  # v1 掩码
    v2_doc = {"rules": [{"path": "name", "action": "allow"},
                        {"path": "email", "action": "deny"}]}
    _req(server, "POST", "/policies/hr/publish", v2_doc)  # v2 拒绝
    data = {"name": "A", "email": "a@x.com"}
    code, body = _req(server, "POST", "/v1/exports",
                      {"data": data, "policy_id": "hr", "purpose": "analytics",
                       "version": 1})
    assert code == 201
    assert body["bundle"]["output"]["email"] == "a***@x.com"


def test_encrypted_export_via_api(server):
    _req(server, "POST", "/policies/hr/publish", POLICY_DOC)
    code, body = _req(server, "POST", "/v1/exports",
                      {"data": {"name": "A", "ssn": "x"},
                       "policy_id": "hr", "purpose": "analytics",
                       "encrypt": True})
    assert code == 201
    assert body["bundle"]["schema_version"] == "mde-encrypted-bundle/v1"
    key = body["encryption_key"]
    code, report = _req(server, "POST", "/v1/exports/verify",
                        {"bundle": body["bundle"], "encryption_key": key,
                         "source_data": {"name": "A", "ssn": "x"}})
    assert code == 200 and report["ok"]


def test_bad_json_and_validation(server):
    code, _ = _req(server, "POST", "/v1/exports",
                   {"data": {}, "policy_id": "nope", "purpose": "analytics"})
    assert code == 404

    # 非法动作在发布期被拒（422）。
    code, err = _req(server, "POST", "/policies/p/publish",
                     {"rules": [{"path": "a", "action": "maybe"}]})
    assert code == 422
