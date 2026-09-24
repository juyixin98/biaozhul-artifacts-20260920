"""HTTP 层与输入校验测试。"""
from __future__ import annotations

from scripts.sighelp import b64e, make_envelope


def test_health_empty(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "root_version": None}


def test_current_root_null_when_empty(client):
    assert client.get("/roots/current").json() is None


def test_invalid_digest_hex_rejected(bootstrapped, parties):
    env = make_envelope(b"x", "t", "1", parties["art_priv"][:2]).model_dump()
    env["digest"] = "zz" * 32
    r = bootstrapped.post(
        "/verify", json={"envelope": env, "content_base64": b64e(b"x")}
    )
    assert r.status_code == 422  # pydantic 校验失败


def test_empty_signatures_rejected(bootstrapped, parties):
    env = make_envelope(b"x", "t", "1", parties["art_priv"][:1]).model_dump()
    env["signatures"] = []
    r = bootstrapped.post("/verify", json={"envelope": env})
    assert r.status_code == 422


def test_extra_field_rejected(bootstrapped, parties):
    env = make_envelope(b"x", "t", "1", parties["art_priv"][:2]).model_dump()
    payload = {"envelope": env, "unexpected": 1}
    r = bootstrapped.post("/verify", json=payload)
    assert r.status_code == 422


def test_invalid_public_pem_in_block(bootstrapped, parties):
    env = make_envelope(b"x", "t", "1", parties["art_priv"][:2]).model_dump()
    env["signatures"][0]["public_key"] = "not-a-pem"
    r = bootstrapped.post("/verify", json={"envelope": env})
    assert r.status_code == 200
    assert r.json()["blocks"][0]["reason"] == "invalid_public_key"
    assert r.json()["accepted"] is False


def test_openapi_available(client):
    spec = client.get("/openapi.json").json()
    paths = set(spec["paths"])
    assert {
        "/health",
        "/roots/current",
        "/roots/bootstrap",
        "/roots/rotate",
        "/verify",
        "/artifacts/register",
    } <= paths
