"""pytest 公共夹具：ASGI in-process 服务 + 已注册签名客户端。"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.crypto import (
    canonical,
    generate_keypair,
    key_id,
    parse_public_pem,
    public_pem,
    verify_object_signature,
)
from app.main import app
from app.store import canonical_envelope


@pytest.fixture()
def api():
    """每个测试重建全新的全局 store（密钥、模拟器、计划、nonce）。"""
    main_module.store.__init__()
    client = TestClient(app)
    yield _SignedTestClient(client, main_module.store)
    client.close()


class _SignedTestClient:
    def __init__(self, http: TestClient, store: main_module.Store):
        self.http = http
        self.store = store
        priv, pub = generate_keypair()
        self.priv, self.pub = priv, pub
        self.kid = key_id(pub)
        info = http.get("/api/server-key").json()
        self.server_pub = parse_public_pem(info["public_key_pem"])
        self.server_kid = info["kid"]
        http.post("/api/client-keys", json={"public_key_pem": public_pem(pub)})

    def envelope(self, payload: dict, ts: int | None = None, nonce: str | None = None) -> dict:
        nonce = nonce or self.http.post("/api/nonces").json()["nonce"]
        ts = ts or int(time.time())
        signable = canonical_envelope(self.kid, nonce, ts, payload)
        return {
            "kid": self.kid,
            "nonce": nonce,
            "ts": ts,
            "payload": payload,
            "sig": self.priv.sign(canonical(signable)).hex(),
        }

    def call(self, method: str, path: str, payload: dict | None = None, envelope: dict | None = None):
        body = envelope or self.envelope(payload or {})
        r = self.http.request(method, path, json=body)
        if r.status_code < 400 and isinstance(r.json(), dict) and "signature" in r.json():
            data = r.json()
            assert data["signature"]["kid"] == self.server_kid
            assert verify_object_signature(
                self.server_pub, data["payload"], data["signature"]["sig"]
            ), "服务器响应签名必须可验签"
            r._wrapped_payload = data["payload"]
        return r

    def payload(self, r) -> dict:
        return getattr(r, "_wrapped_payload", r.json())
