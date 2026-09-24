#!/usr/bin/env python3
"""端到端演示：对运行中的网关(8080)与发行方(8787)发起各种请求并打印结果。

用法（两个服务都已启动后）：
    PYTHONPATH=. python scripts/demo_requests.py
"""

from __future__ import annotations

import base64
import json
import os

import httpx

from cryptography.hazmat.primitives import serialization

from app.jwt_sign import b64u_json

GATEWAY = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8080")
ISSUER = os.environ.get("ISSUER_URL", "http://127.0.0.1:8787")


def mint(**kw: object) -> dict:
    r = httpx.post(f"{ISSUER}/token", json=kw, timeout=10)
    r.raise_for_status()
    return r.json()


def verify(token: str, **kw: object) -> httpx.Response:
    body = {"token": token, **kw}
    return httpx.post(f"{GATEWAY}/verify", json=body, timeout=10)


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(resp: httpx.Response) -> None:
    print(f"HTTP {resp.status_code}")
    print(json.dumps(resp.json(), ensure_ascii=False, indent=2))


def main() -> None:
    # 1. 正常 RS256 -----------------------------------------------------
    banner("1) 正常 RS256 令牌")
    t = mint(issuer="rsa", alg="RS256")["token"]
    show(verify(t))

    # 2. 正常 EdDSA -----------------------------------------------------
    banner("2) 正常 EdDSA 令牌")
    t = mint(issuer="ec", alg="EdDSA")["token"]
    show(verify(t))

    # 3. 正常 HS256（对称，不产生 JWKS 请求） ---------------------------
    banner("3) 正常 HS256 令牌")
    t = mint(issuer="hmac", alg="HS256")["token"]
    show(verify(t))

    # 4. 过期 -----------------------------------------------------------
    banner("4) 已过期令牌 (exp 60s 前)")
    t = mint(issuer="rsa", alg="RS256", expired=True)["token"]
    show(verify(t))

    # 5. nbf 未来 -------------------------------------------------------
    banner("5) 尚未生效 (nbf 在未来 120s)")
    t = mint(issuer="rsa", alg="RS256", nbf_delta=120)["token"]
    show(verify(t))

    # 6. aud 不匹配 -----------------------------------------------------
    banner("6) aud 不匹配")
    t = mint(issuer="rsa", alg="RS256", aud="someone-else")["token"]
    show(verify(t))

    # 7. RS256 -> HS256 算法混淆 ---------------------------------------
    banner("7) 攻击：RS256->HS256 算法混淆（用 RSA 公钥当 HMAC 密钥）")
    jwks = httpx.get(f"{ISSUER}/rsa/jwks.json").json()
    # 用发行方公开的 JWKS 构造 PEM 公钥（攻击者可获得的全部材料）。
    from app.jwks import jwk_to_public_key

    pub, _ = jwk_to_public_key(jwks["keys"][0])
    pem = pub.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    import hmac as _hmac
    import hashlib

    header = b64u_json({"alg": "HS256", "typ": "JWT", "kid": "rsa-1"})
    now = int(__import__("time").time())
    payload = b64u_json({
        "iss": "https://demo-issuer.local/rsa",
        "sub": "attacker", "aud": "api-gateway",
        "iat": now, "exp": now + 300,
    })
    sig = _hmac.new(pem, f"{header}.{payload}".encode(), hashlib.sha256).digest()
    attack = f"{header}.{payload}.{base64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"
    show(verify(attack))

    # 8. jku 指向攻击者地址 --------------------------------------------
    banner("8) 攻击：jku 头指定攻击者取钥地址")
    good = mint(issuer="rsa", alg="RS256")
    token = good["token"]
    h, p, s = token.split(".")
    evil_header = b64u_json({
        "alg": "RS256", "typ": "JWT", "kid": "rsa-1",
        "jku": "https://attacker.example/evil.json",
    })
    show(verify(f"{evil_header}.{p}.{s}"))

    # 9. 未知发行方 ----------------------------------------------------
    banner("9) 未知发行方 iss")
    t = mint(issuer="rsa", alg="RS256")["token"]
    h, p, s = t.split(".")
    raw = json.loads(base64.urlsafe_b64decode(p + "=="))
    raw["iss"] = "https://untrusted.example"
    p2 = b64u_json(raw)
    # 仍用原签名（签名必然也不匹配；结构上先撞到 unknown_issuer）
    show(verify(f"{h}.{p2}.{s}"))

    # 10. 密钥轮换：POST /rotate 后强制刷新网关缓存 --------------------
    banner("10) 发行方轮换密钥 -> 旧令牌失效 -> refresh 后新令牌生效")
    old = mint(issuer="rsa", alg="RS256")
    print("轮换前旧令牌:", verify(old["token"]).status_code)
    httpx.post(f"{ISSUER}/rotate")
    new = mint(issuer="rsa", alg="RS256")
    print("刚轮换、网关未刷新时新令牌结果:")
    show(verify(new["token"]))
    r = httpx.post(f"{GATEWAY}/admin/issuers/demo-rsa/refresh", timeout=10)
    print("强制刷新:", r.status_code, r.json())
    print("刷新后新令牌:", verify(new["token"]).status_code)
    print("刷新后旧令牌:")
    show(verify(old["token"]))

    # 11. 缓存可观测性 --------------------------------------------------
    banner("11) 缓存状态 /admin/issuers")
    r = httpx.get(f"{GATEWAY}/admin/issuers")
    print(json.dumps(r.json(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
