"""算法混淆攻击（acceptance 重点）。

经典 RS256→HS256 混淆：攻击者拿到发行方的 RSA **公钥**（JWKS 本来就是公开的），
把令牌头改成 HS256，用公钥字节当 HMAC 共享密钥签名。验证方若不按发行方
白名单严格区分对称/非对称族，就会把公钥当成“双方共享秘密”从而验签通过。

本服务必须在发行方维度、算法维度、密钥族维度全部拒绝。
"""

from __future__ import annotations

from app.jwt_sign import b64u, sign_jwt

from .conftest import make_issuer, register_jwks, standard_claims


def test_rs256_to_hs256_confusion_rejected(make_env, rsa_key):
    cfg = make_issuer(algs=["RS256"])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})

    # 攻击者视角：只能拿到 PEM 公钥。
    from cryptography.hazmat.primitives import serialization

    pub_pem = rsa_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    claims = standard_claims(now=clock.t)
    attack_token = sign_jwt(
        claims, pub_pem, alg="HS256", kid="k1"
    )
    resp = client.post("/verify", json={"token": attack_token})
    assert resp.status_code == 401, resp.text
    body = resp.json()
    assert body["denied"] is True
    assert body["error"] == "alg_not_allowed"
    assert body["context"]["alg"] == "HS256"
    assert "RS256" in body["context"]["allowed_algs"]


def test_alg_none_always_rejected(make_env, rsa_key):
    cfg = make_issuer(algs=["RS256"])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})

    from app.jwt_sign import b64u_json

    payload = b64u_json(standard_claims(now=clock.t))
    # none 的几种常见大小写/空签名变体。
    for alg_lit, sig in [
        ("none", ""),
        ("None", ""),
        ("NONE", ""),
        ("nOnE", "AA"),
    ]:
        h = b64u_json({"alg": alg_lit, "typ": "JWT", "kid": "k1"})
        token = f"{h}.{payload}.{sig}"
        resp = client.post("/verify", json={"token": token})
        assert resp.status_code == 401
        assert resp.json()["error"] == "alg_not_allowed"


def test_es256_token_against_rsa_issuer_rejected(make_env, rsa_key, es256_key):
    # 发行方只允许 RS256；攻击者用自己的 EC 密钥签 ES256 并把 EC 公钥放进 JWKS
    # （即便投毒成功），算法白名单也必须先挡住。
    cfg = make_issuer(algs=["RS256"])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {
        "k1": (rsa_key, "RS256"),
        "evil": (es256_key, "ES256"),
    })
    token = sign_jwt(
        standard_claims(now=clock.t), es256_key, alg="ES256", kid="evil"
    )
    resp = client.post("/verify", json={"token": token})
    assert resp.json()["error"] == "alg_not_allowed"


def test_key_family_mismatch_after_cache_hit(make_env, rsa_key, es256_key):
    # 发行方同时允许 RS256+ES256，但 JWK 自身带 alg 约束；
    # 用 RSA 的 kid 发 ES256 签名必须被密钥族检查拒绝。
    cfg = make_issuer(algs=["RS256", "ES256"])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {
        "rsa-k": (rsa_key, "RS256"),
        "ec-k": (es256_key, "ES256"),
    })
    # 用 EC 私钥签，但头里 kid 指向 RSA 公钥。
    token = sign_jwt(
        standard_claims(now=clock.t), es256_key, alg="ES256", kid="rsa-k"
    )
    resp = client.post("/verify", json={"token": token})
    assert resp.status_code == 401
    assert resp.json()["error"] in {
        "key_alg_mismatch", "invalid_signature"
    }


def test_jwk_alg_constraint_enforced(make_env, rsa_key):
    # JWK 明确写 alg=RS256，令牌头声称 PS256。
    cfg = make_issuer(algs=["RS256", "PS256"])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})
    token = sign_jwt(
        standard_claims(now=clock.t), rsa_key, alg="PS256", kid="k1"
    )
    resp = client.post("/verify", json={"token": token})
    assert resp.status_code == 401
    assert resp.json()["error"] == "key_alg_mismatch"


def test_hmac_token_for_asymmetric_issuer_never_hits_secret(make_env, rsa_key):
    # 即使发行方配置里“顺带”允许 HS256（混合配置），HS256 也必须使用**服务端
    # 配置**的 HMAC 密钥，而不是 RSA 公钥。
    cfg = make_issuer(
        algs=["RS256", "HS256"],
        hmac_secret_b64=b64u(b"server-side-only-secret-value-32bytes"),
    )
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})

    from cryptography.hazmat.primitives import serialization

    pub_pem = rsa_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    token = sign_jwt(standard_claims(now=clock.t), pub_pem,
                     alg="HS256", kid="k1")
    resp = client.post("/verify", json={"token": token})
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_signature"


def test_unknown_alg_string_rejected(make_env, rsa_key):
    cfg = make_issuer(algs=["RS256"])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})
    from app.jwt_sign import b64u_json

    h = b64u_json({"alg": "RS999", "typ": "JWT", "kid": "k1"})
    p = b64u_json(standard_claims(now=clock.t))
    token = f"{h}.{p}.AAAA"
    resp = client.post("/verify", json={"token": token})
    assert resp.json()["error"] == "alg_not_allowed"
