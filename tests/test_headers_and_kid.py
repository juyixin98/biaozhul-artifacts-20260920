"""头部禁参、kid、发行方索引与重复 kid（acceptance 重点）。"""

from __future__ import annotations

import json

import pytest

from app.errors import ErrCode, VerifyError
from app.jwks import parse_jwks
from app.jwt_sign import (
    b64u_json,
    generate_key,
    jwks_doc,
    public_jwk,
    sign_jwt,
)

from .conftest import make_issuer, register_jwks, standard_claims


@pytest.mark.parametrize("param", ["jku", "jwk", "x5u", "x5c", "x5t", "x5t#S256"])
def test_token_supplied_key_uri_is_refused(make_env, rsa_key, param):
    cfg = make_issuer(algs=["RS256"])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})

    extra = {param: "https://attacker.example/evil-jwks.json"}
    if param == "jwk":
        extra[param] = public_jwk(generate_key("RS256"), "evil")
    token = sign_jwt(
        standard_claims(now=clock.t), rsa_key,
        alg="RS256", kid="k1", extra_headers=extra,
    )
    resp = client.post("/verify", json={"token": token})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "header_parameter_forbidden"
    assert body["context"]["parameter"] == param
    # 关键：攻击者地址绝不能被请求。
    assert not any("attacker.example" in u for u in source.requests)


def test_crit_unrecognized_rejected(make_env, rsa_key):
    cfg = make_issuer(algs=["RS256"])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})
    token = sign_jwt(
        standard_claims(now=clock.t), rsa_key,
        alg="RS256", kid="k1",
        extra_headers={"crit": ["http://attacker.example/hdr"],
                       "http://attacker.example/hdr": 1},
    )
    resp = client.post("/verify", json={"token": token})
    assert resp.json()["error"] == "unrecognized_crit"


def test_missing_kid_rejected(make_env, rsa_key):
    cfg = make_issuer(algs=["RS256"])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})
    h = b64u_json({"alg": "RS256", "typ": "JWT"})
    p = b64u_json(standard_claims(now=clock.t))
    # 用正常签名工具得到签名，再把头换成无 kid 版本。
    good = sign_jwt(standard_claims(now=clock.t), rsa_key,
                    alg="RS256", kid="k1")
    sig = good.split(".")[2]
    resp = client.post("/verify", json={"token": f"{h}.{p}.{sig}"})
    assert resp.json()["error"] == "missing_header_field"


def test_unknown_issuer_rejected(make_env, rsa_key):
    cfg = make_issuer(algs=["RS256"])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})
    token = sign_jwt(
        standard_claims(iss="https://unknown.example", now=clock.t),
        rsa_key, alg="RS256", kid="k1",
    )
    resp = client.post("/verify", json={"token": token})
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"] == "unknown_issuer"
    assert body["context"]["iss"] == "https://unknown.example"
    # 未注册发行方的 URI 绝不能被推断/请求。
    assert source.requests == []


def test_kid_not_in_jwks_rejected(make_env, rsa_key):
    cfg = make_issuer(algs=["RS256"])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})
    token = sign_jwt(
        standard_claims(now=clock.t), rsa_key, alg="RS256", kid="ghost"
    )
    resp = client.post("/verify", json={"token": token})
    assert resp.json()["error"] == "kid_not_found"
    assert resp.json()["context"]["kid"] == "ghost"


def test_duplicate_kid_within_jwks_rejected():
    key = generate_key("RS256")
    doc = jwks_doc(
        public_jwk(key, "dup", alg="RS256"),
        public_jwk(key, "dup", alg="RS256"),
    )
    with pytest.raises(VerifyError) as ei:
        parse_jwks(json.dumps(doc).encode())
    assert ei.value.code == ErrCode.DUPLICATE_KID_IN_JWKS


def test_duplicate_kid_served_by_issuer_denies_and_keeps_old_cache(
    make_env, rsa_key
):
    """轮换投毒：新版本 JWKS 出现重复 kid —— 整份拒绝并继续用旧缓存。"""
    cfg = make_issuer(algs=["RS256"], ttl=10)
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})

    good = sign_jwt(standard_claims(now=clock.t), rsa_key,
                    alg="RS256", kid="k1")
    assert client.post("/verify", json={"token": good}).status_code == 200
    fetched_once = len(source.requests)
    assert fetched_once == 1

    # TTL 到期后发行方返回含重复 kid 的恶意/损坏文档。
    clock.advance(11)
    evil_key = generate_key("RS256")
    source.set(cfg.jwks_uri, jwks_doc(
        public_jwk(rsa_key, "k1", alg="RS256"),
        public_jwk(evil_key, "k1", alg="RS256"),
    ))
    resp = client.post("/verify", json={"token": good})
    # 旧 kid 命中旧缓存 -> 签名仍然成立，降级成功。
    assert resp.status_code == 200, resp.text
    # 新 kid（投毒钥匙）绝不可用：即使坏文档里“碰巧”有它也不行。
    evil_token = sign_jwt(
        standard_claims(now=clock.t), evil_key, alg="RS256", kid="k1"
    )
    resp2 = client.post("/verify", json={"token": evil_token})
    assert resp2.status_code == 401
    assert resp2.json()["error"] == "invalid_signature"


def test_duplicate_json_keys_in_token_rejected(make_env, rsa_key):
    cfg = make_issuer(algs=["RS256"])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})

    from app.jwt_sign import b64u

    # 手搓含重复 iss 的载荷：{"iss":"https://issuer.example",...,"iss":"evil"}
    raw = (
        b'{"iss":"https://issuer.example","sub":"u","aud":"gateway-aud",'
        b'"iat":1000000,"exp":1000300,"iss":"https://evil"}'
    )
    header = b64u_json({"alg": "RS256", "typ": "JWT", "kid": "k1"})
    good = sign_jwt(standard_claims(now=clock.t), rsa_key,
                    alg="RS256", kid="k1")
    sig = good.split(".")[2]
    token = f"{header}.{b64u(raw)}.{sig}"
    resp = client.post("/verify", json={"token": token})
    assert resp.status_code in (400, 401)
    assert resp.json()["error"] == "duplicate_json_key"


def test_private_key_material_in_jwks_rejected():
    key = generate_key("RS256")
    jwk = public_jwk(key, "k1", alg="RS256")
    nums = key.private_numbers()
    jwk["d"] = b64u_json  # 占位，下面正确地填 base64
    from app.jwt_sign import b64u
    size = (nums.public_numbers.n.bit_length() + 7) // 8
    jwk["d"] = b64u(nums.d.to_bytes(size, "big"))
    with pytest.raises(VerifyError) as ei:
        parse_jwks(json.dumps(jwks_doc(jwk)).encode())
    assert ei.value.code == ErrCode.JWKS_KEY_INVALID
