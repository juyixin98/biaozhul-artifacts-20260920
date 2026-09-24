"""exp / nbf 边界时刻、aud、声明类型（acceptance 重点）。

时间语义（RFC 7519 §4.1.4/4.1.5）：
- ``now == exp``        -> 已过期（必须严格早于 exp）
- ``now == exp - 1``    -> 有效
- ``now == nbf``        -> 恰好生效
- ``now == nbf - 1``    -> 尚未生效
leeway 是“向宽容方向”平移窗口，两侧语义不变。
"""

from __future__ import annotations

import pytest

from app.jwt_sign import sign_jwt

from .conftest import make_issuer, register_jwks, standard_claims


def _token_at(key, cfg, clock, *, exp_delta=0, nbf=None, aud="gateway-aud",
              iss="https://issuer.example"):
    claims = standard_claims(
        iss=iss, aud=aud, now=clock.t,
        ttl=0, nbf=(clock.t + nbf) if nbf is not None else None,
    )
    claims["exp"] = int(clock.t) + exp_delta
    return sign_jwt(claims, key, alg="RS256", kid="k1")


@pytest.mark.parametrize("leeway,exp_delta,ok", [
    (0, 0, False),    # now == exp：过期
    (0, 1, True),     # 还有 1 秒：有效
    (0, -1, False),   # 已过期 1 秒
    (5, -5, False),   # 过期恰等于 leeway：仍然过期（now < exp+leeway 不成立）
    (5, -4, True),    # 过期 4 秒，leeway 5：放行
])
def test_exp_boundary(make_env, rsa_key, leeway, exp_delta, ok):
    cfg = make_issuer(algs=["RS256"], leeway=leeway)
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})
    token = _token_at(rsa_key, cfg, clock, exp_delta=exp_delta)
    resp = client.post("/verify", json={"token": token})
    if ok:
        assert resp.status_code == 200, resp.text
    else:
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"] == "token_expired"
        assert "exp" in body["context"] and "now" in body["context"]
        assert body["context"]["leeway_s"] == leeway


@pytest.mark.parametrize("leeway,nbf_delta,ok", [
    (0, 0, True),     # now == nbf：恰好生效
    (0, 1, False),    # 还有 1 秒：拒绝
    (5, 6, False),    # nbf 在未来 6s，leeway 5：拒绝（边界相等不放行）
    (5, 5, True),     # nbf 在未来 5s，leeway 5：放行
    (0, -10, True),   # 早已生效
])
def test_nbf_boundary(make_env, rsa_key, leeway, nbf_delta, ok):
    cfg = make_issuer(algs=["RS256"], leeway=leeway)
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})
    token = _token_at(
        rsa_key, cfg, clock, exp_delta=300, nbf=nbf_delta
    )
    resp = client.post("/verify", json={"token": token})
    if ok:
        assert resp.status_code == 200, resp.text
    else:
        assert resp.status_code == 401
        assert resp.json()["error"] == "token_not_yet_valid"


def test_exp_required(make_env, rsa_key):
    cfg = make_issuer(algs=["RS256"])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})
    claims = standard_claims(now=clock.t)
    del claims["exp"]
    token = sign_jwt(claims, rsa_key, alg="RS256", kid="k1")
    resp = client.post("/verify", json={"token": token})
    assert resp.status_code == 400
    assert resp.json()["error"] == "missing_payload_claim"


def test_aud_string_mismatch(make_env, rsa_key):
    cfg = make_issuer(algs=["RS256"], audiences=["expected"])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})
    token = _token_at(rsa_key, cfg, clock, exp_delta=300, aud="someone-else")
    resp = client.post("/verify", json={"token": token})
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"] == "aud_not_allowed"
    assert body["context"]["expected_any_of"] == ["expected"]


def test_aud_list_no_intersection(make_env, rsa_key):
    cfg = make_issuer(algs=["RS256"], audiences=["a", "b"])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})
    claims = standard_claims(aud=["x", "y"], now=clock.t)
    token = sign_jwt(claims, rsa_key, alg="RS256", kid="k1")
    resp = client.post("/verify", json={"token": token})
    assert resp.json()["error"] == "aud_not_allowed"


def test_non_numeric_exp_rejected(make_env, rsa_key):
    cfg = make_issuer(algs=["RS256"])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})
    claims = standard_claims(now=clock.t)
    claims["exp"] = "1000300"
    token = sign_jwt(claims, rsa_key, alg="RS256", kid="k1")
    resp = client.post("/verify", json={"token": token})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "bad_claim_type"
    assert body["context"]["actual_type"] == "str"


def test_boolean_exp_rejected(make_env, rsa_key):
    cfg = make_issuer(algs=["RS256"])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})
    claims = standard_claims(now=clock.t)
    claims["exp"] = True
    token = sign_jwt(claims, rsa_key, alg="RS256", kid="k1")
    resp = client.post("/verify", json={"token": token})
    assert resp.json()["error"] == "bad_claim_type"


def test_future_iat_rejected(make_env, rsa_key):
    cfg = make_issuer(algs=["RS256"], leeway=0)
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})
    claims = standard_claims(now=clock.t)
    claims["iat"] = int(clock.t) + 60
    claims["nbf"] = int(clock.t) + 60
    token = sign_jwt(claims, rsa_key, alg="RS256", kid="k1")
    resp = client.post("/verify", json={"token": token})
    assert resp.json()["error"] == "token_not_yet_valid"
