"""HTTP 接口测试（FastAPI TestClient）：决策接口、错误码、验签、体积限制。"""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from app import main as web
from app.crypto import generate_keypair, private_key_to_pem, public_key_to_pem, sign_policies


@pytest.fixture()
def trusted_key(tmp_path, monkeypatch):
    priv, pub = generate_keypair()
    pub_path = tmp_path / "public.pem"
    priv_path = tmp_path / "private.pem"
    pub_path.write_bytes(public_key_to_pem(pub))
    priv_path.write_bytes(private_key_to_pem(priv))
    monkeypatch.setattr(web, "_TRUSTED_PUBLIC_KEY_PATH", str(pub_path))
    return priv, pub


@pytest.fixture()
def client():
    with TestClient(web.app) as c:
        yield c


POLICIES = [
    {"id": "p1", "rules": [
        {"id": "a1", "effect": "allow",
         "condition": {"op": "eq", "args": [{"attr": "subject.dept"}, {"literal": "eng"}]}},
        {"id": "d1", "effect": "deny",
         "condition": {"op": "eq", "args": [{"attr": "resource.kind"}, {"literal": "secret"}]}},
    ]},
]


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_evaluate_allow(client):
    r = client.post("/v1/evaluate", json={
        "policies": POLICIES,
        "subject": {"dept": "eng"},
        "resource": {"kind": "public"},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"] == "ALLOW"
    assert body["minimal_relevant_rules"] == ["a1"]
    assert body["combining_algorithm"] == "deny-overrides"
    assert all(set(rr) == {"rule_id", "policy_id", "effect", "condition_value", "fired"}
               for rr in body["rule_results"])


def test_evaluate_deny_overrides_allow(client):
    r = client.post("/v1/evaluate", json={
        "policies": POLICIES,
        "subject": {"dept": "eng"},
        "resource": {"kind": "secret"},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"] == "DENY"
    assert body["minimal_relevant_rules"] == ["d1"]
    assert body["conflicting_allow_rules"] == ["a1"]


def test_evaluate_unknown_attribute_default_deny(client):
    r = client.post("/v1/evaluate", json={
        "policies": POLICIES,
        "subject": {},          # dept 缺失
        "resource": {"kind": "public"},
    })
    body = r.json()
    assert body["decision"] == "DENY"
    assert body["reason"] == "no_applicable_rule"
    assert body["counts"]["indeterminate"] == 1


def test_evaluate_invalid_policy_returns_structured_400(client):
    r = client.post("/v1/evaluate", json={
        "policies": [{"id": "p1", "rules": [
            {"id": "bad", "effect": "allow",
             "condition": {"op": "eval", "args": []}}]}],
        "subject": {}, "resource": {},
    })
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "invalid_policy"
    assert err["details"]


def test_evaluate_malformed_json_422(client):
    r = client.post("/v1/evaluate", content=b"{not-json",
                    headers={"content-type": "application/json"})
    assert r.status_code in (400, 422)


def test_signed_evaluate_happy_path(client, trusted_key):
    priv, _pub = trusted_key
    bundle = sign_policies(POLICIES, priv, kid="k-1")
    r = client.post("/v1/evaluate-signed", json={
        "bundle": bundle,
        "subject": {"dept": "eng"},
        "resource": {"kind": "public"},
    })
    assert r.status_code == 200, r.text
    assert r.json()["decision"] == "ALLOW"


def test_signed_evaluate_rejects_tampering(client, trusted_key):
    priv, _pub = trusted_key
    bundle = sign_policies(POLICIES, priv, kid="k-1")
    bundle["policies"][0]["rules"][0]["effect"] = "deny"  # 篡改后不重签
    r = client.post("/v1/evaluate-signed", json={
        "bundle": bundle, "subject": {}, "resource": {}})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "invalid_signature"


def test_signed_evaluate_rejects_untrusted_key(client, trusted_key):
    _priv, _pub = trusted_key
    attacker_priv, _ = generate_keypair()
    bundle = sign_policies(POLICIES, attacker_priv, kid="attacker")
    r = client.post("/v1/evaluate-signed", json={
        "bundle": bundle, "subject": {}, "resource": {}})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "invalid_signature"


def test_signed_evaluate_bad_bundle_structure_400(client, trusted_key):
    priv, _pub = trusted_key
    bundle = sign_policies(POLICIES, priv, kid="k-1")
    bundle["alg"] = "HS256"
    r = client.post("/v1/evaluate-signed", json={
        "bundle": bundle, "subject": {}, "resource": {}})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_bundle"


def test_signed_endpoint_without_trust_configuration(client, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "_TRUSTED_PUBLIC_KEY_PATH", str(tmp_path / "missing.pem"))
    r = client.post("/v1/evaluate-signed", json={"bundle": {}, "subject": {}, "resource": {}})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "trust_not_configured"


def test_trust_endpoint_reports_key_fingerprint(client, trusted_key):
    r = client.get("/v1/trust")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["alg"] == "Ed25519"
    assert len(body["sha256"]) == 64


def test_body_size_limit_enforced(client, monkeypatch):
    monkeypatch.setattr(web, "_MAX_BODY", 1024)
    big = {"policies": POLICIES, "subject": {"blob": "x" * 4096}, "resource": {}}
    r = client.post("/v1/evaluate", json=big)
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"
