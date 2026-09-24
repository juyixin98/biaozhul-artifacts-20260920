"""端到端：真实 HTTP 本地服务提供 JWKS，走生产用 UrllibJwksFetcher。

验证“禁止由令牌指定取钥地址”的完整链路：网关只向**配置中的**
127.0.0.1 JWKS 端点发起请求，令牌头里的 jku 不会产生任何外联。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.config import IssuerConfig
from app.jwks import UrllibJwksFetcher, JwksCacheRegistry
from app.jwt_sign import generate_key, jwks_doc, public_jwk, sign_jwt
from app.main import create_app
from fastapi.testclient import TestClient

from .conftest import FakeClock, standard_claims


class _Handler(BaseHTTPRequestHandler):
    docs: dict[str, bytes] = {}
    access_log: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        type(self).access_log.append(self.path)
        body = type(self).docs.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "max-age=60")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a: object) -> None:
        pass


@pytest.fixture
def jwks_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    _Handler.docs = {}
    _Handler.access_log = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, _Handler
    server.shutdown()
    server.server_close()


def _app(port: str, clock: FakeClock, algs=None) -> TestClient:
    cfg = IssuerConfig(
        id="local",
        iss="https://real-issuer.example",
        jwks_uri=f"http://127.0.0.1:{port}/jwks.json",
        allowed_algs=frozenset(algs or ["RS256"]),
        audiences=frozenset({"gateway-aud"}),
        leeway=0,
        cache_ttl=60,
    )
    from app.config import TrustStore

    store = TrustStore(issuers={"local": cfg}, by_iss={cfg.iss: cfg})
    app = create_app(
        store,
        registry=JwksCacheRegistry(
            fetcher=UrllibJwksFetcher(), clock=clock
        ),
        clock=clock,
    )
    return TestClient(app), cfg


def test_real_http_jwks_roundtrip(jwks_server):
    server, handler = jwks_server
    port = server.server_address[1]
    clock = FakeClock()
    client, cfg = _app(port, clock)

    key = generate_key("RS256")
    doc = json.dumps(jwks_doc(public_jwk(key, "live", alg="RS256"))).encode()
    handler.docs["/jwks.json"] = doc

    token = sign_jwt(
        standard_claims(iss=cfg.iss, now=clock.t),
        key, alg="RS256", kid="live",
    )
    resp = client.post("/verify", json={"token": token})
    assert resp.status_code == 200, resp.text
    assert handler.access_log.count("/jwks.json") == 1

    # 第二个请求命中缓存，不再回源。
    client.post("/verify", json={"token": token})
    assert handler.access_log.count("/jwks.json") == 1


def test_real_http_jku_header_does_not_drive_fetch(jwks_server):
    server, handler = jwks_server
    port = server.server_address[1]
    clock = FakeClock()
    client, cfg = _app(port, clock)

    key = generate_key("RS256")
    handler.docs["/jwks.json"] = json.dumps(
        jwks_doc(public_jwk(key, "live", alg="RS256"))
    ).encode()

    token = sign_jwt(
        standard_claims(iss=cfg.iss, now=clock.t),
        key, alg="RS256", kid="live",
        extra_headers={"jku": f"http://127.0.0.1:{port}/NEVER-FETCHED"},
    )
    resp = client.post("/verify", json={"token": token})
    assert resp.status_code == 400
    assert resp.json()["error"] == "header_parameter_forbidden"
    assert "/NEVER-FETCHED" not in handler.access_log
    # 在结构检查阶段就被拒，连配置内 JWKS 都不会拉取。
    assert "/jwks.json" not in handler.access_log


def test_real_http_rotation_via_admin_refresh(jwks_server):
    server, handler = jwks_server
    port = server.server_address[1]
    clock = FakeClock()
    client, cfg = _app(port, clock)

    old = generate_key("RS256")
    new = generate_key("RS256")
    handler.docs["/jwks.json"] = json.dumps(
        jwks_doc(public_jwk(old, "k", alg="RS256"))
    ).encode()

    t_old = sign_jwt(standard_claims(iss=cfg.iss, now=clock.t),
                     old, alg="RS256", kid="k")
    assert client.post("/verify", json={"token": t_old}).status_code == 200

    handler.docs["/jwks.json"] = json.dumps(
        jwks_doc(public_jwk(new, "k", alg="RS256"))
    ).encode()
    r = client.post("/admin/issuers/local/refresh")
    assert r.status_code == 200 and r.json()["keys"] == 1

    t_new = sign_jwt(standard_claims(iss=cfg.iss, now=clock.t),
                     new, alg="RS256", kid="k")
    assert client.post("/verify", json={"token": t_new}).status_code == 200
    assert client.post(
        "/verify", json={"token": t_old}
    ).status_code == 401
