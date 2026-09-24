"""pytest 公共夹具：每个测试前重置全局引擎，并提供 HMAC 签名的同步 ASGI 客户端。"""
from __future__ import annotations

import json
import time

import pytest
from starlette.testclient import TestClient

from app.crypto import sign_request
from app.main import app, engine

SECRET = "test-secret"


@pytest.fixture(autouse=True)
def _reset_engine(monkeypatch):
    monkeypatch.setenv("FUSER_HMAC_SECRET", SECRET)
    from app import config, crypto

    monkeypatch.setattr(config.settings, "HMAC_SECRET", SECRET)
    monkeypatch.setattr(crypto.settings, "HMAC_SECRET", SECRET)
    engine.reset()
    yield
    engine.reset()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def signed():
    def _signed(method: str, path: str, payload=None, *, timestamp=None, secret=SECRET):
        raw = (
            json.dumps(payload, separators=(",", ":")).encode()
            if payload is not None
            else b""
        )
        ts = timestamp if timestamp is not None else f"{time.time():.6f}"
        headers, _ = sign_request(method, path, raw, secret=secret, timestamp=ts)
        headers["Content-Type"] = "application/json"
        return raw, headers

    return _signed
