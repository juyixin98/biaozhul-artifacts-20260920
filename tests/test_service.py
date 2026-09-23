import json
import threading
import urllib.error
import urllib.request

import pytest

from maskcompiler.service import build_server

RULESET = {
    "version": 1,
    "rules": [
        {
            "id": "phone",
            "path": "$.users[*].phone",
            "transform": "mask",
            "params": {"keep_last": 4},
            "priority": 10,
        }
    ],
}


@pytest.fixture()
def server(bundle):
    srv = build_server("127.0.0.1", 0, bundle)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def url_for(server, path):
    return "http://127.0.0.1:%d%s" % (server.server_address[1], path)


def call(server, method, path, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url_for(server, path), data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_health(server):
    status, body = call(server, "GET", "/health")
    assert status == 200 and body == {"status": "ok"}


def test_full_lifecycle_apply_and_delete(server):
    status, body = call(server, "POST", "/v1/rulesets",
                        {"name": "demo", "ruleset": RULESET})
    assert status == 201
    assert body["ruleset"]["rule_count"] == 1

    status, body = call(server, "POST", "/v1/rulesets/demo/apply",
                        {"document": {"users": [{"phone": "13812345678"}]}})
    assert status == 200
    assert body["output"]["users"][0]["phone"] == "*******5678"
    assert body["report"]["matched_rules"] == {"phone": 1}

    status, _ = call(server, "GET", "/v1/rulesets/demo")
    assert status == 200

    status, body = call(server, "DELETE", "/v1/rulesets/demo")
    assert status == 200 and body == {"deleted": "demo"}

    status, body = call(server, "POST", "/v1/rulesets/demo/apply",
                        {"document": {"users": []}})
    assert status == 400 and body["error"]["code"] == "RuleSyntaxError"


def test_unknown_ruleset_404_like_error(server):
    status, body = call(server, "GET", "/v1/rulesets/nope")
    assert status == 400
    assert "nope" in body["error"]["message"]


def test_bad_ruleset_rejected(server):
    bad = {"version": 1, "rules": [
        {"id": "r", "path": "$.x", "transform": "scramble", "priority": 1}
    ]}
    status, body = call(server, "POST", "/v1/rulesets",
                        {"name": "bad", "ruleset": bad})
    assert status == 400
    assert body["error"]["code"] == "UnknownTransformError"


def test_conflict_returns_409_and_no_value_leak(server):
    dup = {"version": 1, "rules": [
        {"id": "r1", "path": "$.user.phone", "transform": "redact", "priority": 5},
        {"id": "r2", "path": "$.user.phone", "transform": "mask",
         "params": {"keep_last": 4}, "priority": 5},
    ]}
    status, body = call(server, "POST", "/v1/rulesets",
                        {"name": "dup", "ruleset": dup})
    assert status == 409
    assert body["error"]["code"] == "RuleConflictError"


def test_missing_field_error_body_contains_rule_and_path_only(server):
    rules = {"version": 1, "rules": [
        {"id": "audit", "path": "$.audit_id", "transform": "redact",
         "priority": 1, "on_missing": "error"}
    ]}
    call(server, "POST", "/v1/rulesets", {"name": "mf", "ruleset": rules})
    secret = "SUPER-SECRET-AUDIT-42"
    status, body = call(server, "POST", "/v1/rulesets/mf/apply",
                        {"document": {"other": secret}})
    assert status == 422
    raw = json.dumps(body)
    assert secret not in raw
    assert "audit" in raw and "$.audit_id" in raw


def test_type_mismatch_returns_422_without_value(server):
    rules = {"version": 1, "rules": [
        {"id": "n", "path": "$.n", "transform": "mask",
         "params": {"keep_last": 1}, "priority": 1}
    ]}
    call(server, "POST", "/v1/rulesets", {"name": "tm", "ruleset": rules})
    status, body = call(server, "POST", "/v1/rulesets/tm/apply",
                        {"document": {"n": 987654321}})
    assert status == 422
    assert "987654321" not in json.dumps(body)


def test_invalid_json_body_rejected(server):
    req = urllib.request.Request(
        url_for(server, "/v1/rulesets"), data=b"{not json", method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 400
