"""共享测试固件：真实 Ed25519 密钥、内存服务、签名助手。"""
from __future__ import annotations

import json

import pytest
from nacl.signing import SigningKey

from app import crypto
from app.config import Settings
from app.repository import MemoryRepository
from app.service import ReplayService


@pytest.fixture(scope="session")
def feeder_kp():
    return crypto.generate_keypair()


@pytest.fixture(scope="session")
def server_kp():
    return crypto.generate_keypair()


@pytest.fixture
def mem_repo() -> MemoryRepository:
    return MemoryRepository()


def make_settings(**overrides) -> Settings:
    base = dict(
        database_url="",
        require_signatures=True,
        feeder_public_key_hex="",
        server_private_key_hex="",
        keys_dir="/nonexistent",
        seconds_per_year=365 * 24 * 3600,
        price_staleness_seconds=60,
        max_repay_fraction_num=1,
        max_repay_fraction_den=2,
        allow_reset=True,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def service(mem_repo, feeder_kp, server_kp) -> ReplayService:
    s = make_settings()
    return ReplayService(
        mem_repo,
        s,
        verify_key=feeder_kp.signing_key.verify_key,
        signing_key=server_kp.signing_key,
    )


@pytest.fixture
def settings():
    return make_settings()


def sign_event(sk: SigningKey, event_id: str, ts: int, etype: str, payload: dict) -> dict:
    ev = {"event_id": event_id, "ts": ts, "type": etype, "payload": payload}
    ev["sig"] = sk.sign(crypto.event_signing_bytes(ev)).signature.hex()
    return ev


@pytest.fixture
def sign(feeder_kp):
    def _w(event_id, ts, etype, payload):
        return sign_event(feeder_kp.signing_key, event_id, ts, etype, payload)

    return _w


def load_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@pytest.fixture
def client_factory(mem_repo, feeder_kp, server_kp):
    """生成带注入依赖的 httpx ASGI 客户端。"""
    from httpx import ASGITransport, AsyncClient

    from app.api import create_app

    def _factory(settings=None):
        s = settings or make_settings()
        application = create_app(
            s,
            verify_key=feeder_kp.signing_key.verify_key,
            signing_key=server_kp.signing_key,
            repo=mem_repo,
        )
        transport = ASGITransport(app=application)
        client = AsyncClient(transport=transport, base_url="http://test")
        # 不走 lifespan，直接注入装配好的状态
        application.state.verify_key = feeder_kp.signing_key.verify_key
        application.state.signing_key = server_kp.signing_key
        application.state.engine = None
        application.state.session_maker = None
        application.state.memory_repo = mem_repo
        return client

    return _factory
