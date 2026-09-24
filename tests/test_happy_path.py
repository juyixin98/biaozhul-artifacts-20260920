"""正常路径与算法覆盖：RSA / PSS / EC / EdDSA / HMAC。"""

from __future__ import annotations

import base64

import pytest

from app.jwt_sign import b64u, generate_key, sign_jwt

from .conftest import make_issuer, register_jwks, standard_claims


def _verify_ok(client, token):
    resp = client.post("/verify", json={"token": token})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is True
    return body


@pytest.mark.parametrize("alg", ["RS256", "RS384", "RS512", "PS256"])
def test_rsa_family_accepted(make_env, alg):
    key = generate_key(alg)
    cfg = make_issuer(algs=[alg])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"key-1": (key, alg)})

    token = sign_jwt(
        standard_claims(now=clock.t), key, alg=alg, kid="key-1"
    )
    body = _verify_ok(client, token)
    assert body["alg"] == alg
    assert body["kid"] == "key-1"
    assert body["claims"]["sub"] == "user-123"


@pytest.mark.parametrize("alg", ["ES256", "ES384", "ES512", "EdDSA"])
def test_ec_and_eddsa_accepted(make_env, alg):
    key = generate_key(alg)
    cfg = make_issuer(algs=[alg])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k-ec": (key, alg)})

    token = sign_jwt(
        standard_claims(now=clock.t), key, alg=alg, kid="k-ec"
    )
    body = _verify_ok(client, token)
    assert body["alg"] == alg


def test_hs256_accepted_without_jwks(make_env):
    secret = b"a-very-long-shared-secret-value-32b!!"
    cfg = make_issuer(
        id="hmac-issuer",
        iss="https://hmac.example",
        algs=["HS256"],
        audiences=["svc"],
        hmac_secret_b64=b64u(secret),
    )
    _, client, store, source, clock = make_env([cfg])
    # 对称发行方：不应该发起任何 JWKS 请求。
    assert source.requests == []

    token = sign_jwt(
        standard_claims(iss="https://hmac.example", aud="svc", now=clock.t),
        secret,
        alg="HS256",
        kid="local-key",
    )
    body = _verify_ok(client, token)
    assert body["issuer_id"] == "hmac-issuer"
    assert source.requests == []


def test_aud_list_with_match(make_env, rsa_key):
    cfg = make_issuer(audiences=["a1", "a2"])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})
    claims = standard_claims(aud=["other", "a2"], now=clock.t)
    token = sign_jwt(claims, rsa_key, alg="RS256", kid="k1")
    _verify_ok(client, token)


def test_expected_aud_request_parameter(make_env, rsa_key):
    cfg = make_issuer(audiences=["a1", "a2"])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})
    token = sign_jwt(
        standard_claims(aud=["a1", "a2"], now=clock.t),
        rsa_key, alg="RS256", kid="k1",
    )
    resp = client.post(
        "/verify", json={"token": token, "expected_aud": "a2"}
    )
    assert resp.status_code == 200
    resp = client.post(
        "/verify", json={"token": token, "expected_aud": "missing"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "aud_not_allowed"


def test_signed_content_tampered_rejected(make_env, rsa_key):
    cfg = make_issuer(algs=["RS256"])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})
    claims = standard_claims(now=clock.t)
    # 同长度的 sub 值，方便做等长字节替换，篡改后仍是“合法 JSON 但内容不同”。
    claims["sub"] = "user-AAA"
    token = sign_jwt(claims, rsa_key, alg="RS256", kid="k1")
    h, p, s = token.split(".")

    raw_p = base64.urlsafe_b64decode(p + "=" * (-len(p) % 4))
    assert b"user-AAA" in raw_p
    tampered = raw_p.replace(b"user-AAA", b"user-BBB", 1)
    p2 = base64.urlsafe_b64encode(tampered).rstrip(b"=").decode()
    resp = client.post("/verify", json={"token": f"{h}.{p2}.{s}"})
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_signature"


def test_issuers_endpoint_hides_secrets(make_env):
    cfg = make_issuer(
        id="h", iss="https://h", algs=["HS256"],
        hmac_secret_b64=b64u(b"x" * 48),
    )
    _, client, *_ = make_env([cfg])
    resp = client.get("/issuers")
    text = resp.text
    assert "hmac_secret" not in text
    assert resp.json()["issuers"][0]["symmetric"] is True
