"""JWKS 按发行方隔离、轮换缓存、TTL 与故障降级（acceptance 重点）。"""

from __future__ import annotations

from app.jwt_sign import generate_key, sign_jwt

from .conftest import make_issuer, register_jwks, standard_claims


def test_caches_are_isolated_per_issuer(make_env):
    key_a = generate_key("RS256")
    key_b = generate_key("RS256")
    cfg_a = make_issuer(
        id="issuer-a", iss="https://a.example",
        jwks_uri="https://a.example/jwks",
    )
    cfg_b = make_issuer(
        id="issuer-b", iss="https://b.example",
        jwks_uri="https://b.example/jwks",
    )
    _, client, _, source, clock = make_env([cfg_a, cfg_b])
    register_jwks(source, cfg_a, {"shared-kid": (key_a, "RS256")})
    register_jwks(source, cfg_b, {"shared-kid": (key_b, "RS256")})

    # 两个发行方故意使用相同 kid：各自必须取到自己的钥匙。
    token_a = sign_jwt(
        standard_claims(iss="https://a.example", now=clock.t),
        key_a, alg="RS256", kid="shared-kid",
    )
    token_b = sign_jwt(
        standard_claims(iss="https://b.example", now=clock.t),
        key_b, alg="RS256", kid="shared-kid",
    )
    assert client.post("/verify", json={"token": token_a}).json()[
        "issuer_id"
    ] == "issuer-a"
    assert client.post("/verify", json={"token": token_b}).json()[
        "issuer_id"
    ] == "issuer-b"

    # 用 A 的私钥签 B 的 iss，必然失败（且不是缓存串台）。
    cross = sign_jwt(
        standard_claims(iss="https://b.example", now=clock.t),
        key_a, alg="RS256", kid="shared-kid",
    )
    resp = client.post("/verify", json={"token": cross})
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_signature"


def test_key_rotation_within_ttl_uses_old_key_after_then_new(make_env):
    old_key = generate_key("RS256")
    new_key = generate_key("RS256")
    cfg = make_issuer(algs=["RS256"], ttl=100)
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (old_key, "RS256")})

    old_token = sign_jwt(
        standard_claims(now=clock.t), old_key, alg="RS256", kid="k1"
    )
    assert client.post("/verify", json={"token": old_token}).status_code == 200

    # 发行方轮换（同一 kid 换新公钥），但缓存未过期 -> 仍按旧缓存验签，
    # 新签名会被拒（这是“缓存窗口内旧 kid 继续有效”的预期行为）。
    register_jwks(source, cfg, {"k1": (new_key, "RS256")})
    new_token = sign_jwt(
        standard_claims(now=clock.t), new_key, alg="RS256", kid="k1"
    )
    assert client.post(
        "/verify", json={"token": new_token}
    ).json()["error"] == "invalid_signature"

    # 管理接口强制刷新后，新钥匙生效。
    r = client.post("/admin/issuers/rsa-issuer/refresh")
    assert r.status_code == 200 and r.json()["keys"] == 1
    assert client.post("/verify", json={"token": new_token}).status_code == 200
    assert client.post(
        "/verify", json={"token": old_token}
    ).json()["error"] == "invalid_signature"


def test_new_kid_picked_up_after_expiry(make_env):
    key1 = generate_key("RS256")
    key2 = generate_key("RS256")
    cfg = make_issuer(algs=["RS256"], ttl=10)
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (key1, "RS256")})

    t1 = sign_jwt(standard_claims(now=clock.t), key1,
                  alg="RS256", kid="k1")
    assert client.post("/verify", json={"token": t1}).status_code == 200

    # 发行方双钥匙并存（滚动轮换），缓存过期后应同时认识 k1 与 k2。
    register_jwks(source, cfg, {
        "k1": (key1, "RS256"), "k2": (key2, "RS256")
    })
    t2 = sign_jwt(standard_claims(now=clock.t), key2,
                  alg="RS256", kid="k2")
    # 缓存新鲜时不认识 k2，也不会因为一次 miss 反复刷新新鲜缓存。
    resp = client.post("/verify", json={"token": t2})
    assert resp.json()["error"] == "kid_not_found"

    clock.advance(11)
    assert client.post("/verify", json={"token": t2}).status_code == 200
    assert client.post("/verify", json={"token": t1}).status_code == 200


def test_fetch_failure_with_empty_cache_denies(make_env):
    cfg = make_issuer(algs=["RS256"], ttl=10)
    _, client, _, source, clock = make_env([cfg])
    source.fail(cfg.jwks_uri)
    key = generate_key("RS256")
    token = sign_jwt(standard_claims(now=clock.t), key,
                     alg="RS256", kid="k1")
    resp = client.post("/verify", json={"token": token})
    assert resp.status_code == 401
    assert resp.json()["error"] == "jwks_fetch_failed"


def test_fetch_failure_falls_back_to_stale_cache(make_env):
    key = generate_key("RS256")
    cfg = make_issuer(algs=["RS256"], ttl=10)
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (key, "RS256")})
    token = sign_jwt(standard_claims(now=clock.t), key,
                     alg="RS256", kid="k1")
    assert client.post("/verify", json={"token": token}).status_code == 200

    clock.advance(11)
    source.fail(cfg.jwks_uri)
    # 过期 + 拉取失败：旧 kid 仍可降级验签。
    resp = client.post("/verify", json={"token": token})
    assert resp.status_code == 200

    snap = client.get("/admin/issuers").json()["caches"]["rsa-issuer"]
    assert snap["cached_keys"] == 1


def test_cache_observability_endpoint(make_env):
    cfg = make_issuer(algs=["RS256"], ttl=10)
    _, client, _, source, clock = make_env([cfg])
    key = generate_key("RS256")
    register_jwks(source, cfg, {"abc": (key, "RS256")})
    token = sign_jwt(standard_claims(now=clock.t), key,
                     alg="RS256", kid="abc")
    client.post("/verify", json={"token": token})
    snap = client.get("/admin/issuers").json()["caches"]["rsa-issuer"]
    assert snap["cached_kids"] == ["abc"]
    assert snap["fresh"] is True
    clock.advance(11)
    snap = client.get("/admin/issuers").json()["caches"]["rsa-issuer"]
    assert snap["fresh"] is False


def test_cache_control_max_age_shortens_ttl(make_env):
    key = generate_key("RS256")
    cfg = make_issuer(algs=["RS256"], ttl=100)
    _, client, store, source, clock = make_env([cfg])
    # FakeJwksSource 默认回 max-age=300（不会拉长配置 TTL）。
    register_jwks(source, cfg, {"k1": (key, "RS256")})
    token = sign_jwt(standard_claims(now=clock.t), key,
                     alg="RS256", kid="k1")
    client.post("/verify", json={"token": token})
    snap = client.get("/admin/issuers").json()["caches"]["rsa-issuer"]
    assert snap["ttl_s"] == 100
