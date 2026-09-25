import json
import threading
from pathlib import Path

import pytest

from mde.api import build_server

POLICY = {
    "version": "mde/policy@v1",
    "policy_id": "api-demo",
    "revision": 1,
    "purposes": {
        "analytics": {
            "default": "deny",
            "allow": ["id"],
            "deny": ["ssn"],
            "generalize": {"email": {"transform": "email_domain", "params": {}}},
        }
    },
    "aliases": {},
}
RECORDS = [{"id": "C-1", "email": "a@example.com", "ssn": "S"}]


@pytest.fixture()
def server(tmp_path: Path):
    httpd = build_server("127.0.0.1", 0, tmp_path / "data", quiet=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _request(httpd, method: str, path: str, body=None):
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
    payload = json.dumps(body) if body is not None else None
    conn.request(method, path, body=payload,
                 headers={"Content-Type": "application/json"} if payload else {})
    resp = conn.getresponse()
    data = json.loads(resp.read().decode())
    conn.close()
    return resp.status, data


def test_api_full_flow(server):
    status, health = _request(server, "GET", "/health")
    assert status == 200 and health["status"] == "ok"

    status, pub = _request(server, "POST", "/policies", POLICY)
    assert status == 200, pub
    fp = pub["policy_fingerprint"]

    # 重复发布幂等
    status, pub2 = _request(server, "POST", "/policies", POLICY)
    assert status == 200 and pub2["policy_fingerprint"] == fp

    status, revs = _request(server, "GET", "/policies/api-demo")
    assert status == 200 and revs["revisions"][0]["policy_fingerprint"] == fp

    status, pkg = _request(server, "POST", "/exports", {
        "purpose": "analytics", "policy_fingerprint": fp, "records": RECORDS,
    })
    assert status == 200
    assert pkg["output"][0] == {"id": "C-1", "email": "example.com"}

    status, rep = _request(server, "POST", "/verify", {"package": pkg})
    assert status == 200 and rep["overall_passed"]

    # 端到端复验（本地密钥 + 原始记录）
    status, rep2 = _request(server, "POST", "/verify", {
        "package": pkg, "records": RECORDS, "use_local_keys": True,
    })
    assert status == 200 and rep2["overall_passed"], rep2


def test_api_rejects_bad_policy_and_unknown_fingerprint(server):
    bad = {"version": "mde/policy@v1", "policy_id": "x", "revision": 1,
           "purposes": {}}
    status, err = _request(server, "POST", "/policies", bad)
    assert status == 400 and "校验" in err["error"]

    status, err = _request(server, "POST", "/exports", {
        "purpose": "analytics", "policy_fingerprint": "deadbeef",
        "records": RECORDS,
    })
    assert status == 404
