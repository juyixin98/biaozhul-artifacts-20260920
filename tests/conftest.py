"""pytest 夹具：可控时钟、内存 JWKS 源、造令牌助手。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.config import IssuerConfig, TrustStore
from app.jwks import FetchedJwks, JwksCacheRegistry
from app.jwt_sign import (
    b64u,
    generate_key,
    jwks_doc,
    public_jwk,
)
from app.main import create_app


class FakeClock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@dataclass
class FakeJwksSource:
    """按 URI 提供 JWKS 文档；可随时轮换、可模拟故障。"""

    docs: dict[str, dict[str, Any]] = field(default_factory=dict)
    fail_uris: set[str] = field(default_factory=set)
    requests: list[str] = field(default_factory=list)

    def set(self, uri: str, doc: dict[str, Any]) -> None:
        self.docs[uri] = doc

    def fail(self, uri: str) -> None:
        self.fail_uris.add(uri)

    def unfail(self, uri: str) -> None:
        self.fail_uris.discard(uri)

    def __call__(self, uri: str) -> FetchedJwks:
        self.requests.append(uri)
        if uri in self.fail_uris or uri not in self.docs:
            from app.errors import ErrCode, VerifyError

            raise VerifyError(
                ErrCode.JWKS_FETCH_FAILED, "FakeJwksSource: 端点故障/缺失"
            )
        return FetchedJwks(
            body=json.dumps(self.docs[uri]).encode("utf-8"),
            headers={"cache-control": "max-age=300"},
        )


def make_issuer(
    *,
    id: str = "rsa-issuer",
    iss: str = "https://issuer.example",
    jwks_uri: str = "https://issuer.example/jwks.json",
    algs: list[str] | None = None,
    audiences: list[str] | None = None,
    leeway: int = 0,
    ttl: int = 300,
    hmac_secret_b64: str | None = None,
) -> IssuerConfig:
    if algs is None:
        algs = ["RS256", "ES256", "PS256", "EdDSA"]
    if audiences is None:
        audiences = ["gateway-aud"]
    if hmac_secret_b64 is None and all(a.startswith("HS") for a in algs):
        hmac_secret_b64 = b64u(b"x" * 48)
    return IssuerConfig(
        id=id,
        iss=iss,
        allowed_algs=frozenset(algs),
        audiences=frozenset(audiences),
        leeway=leeway,
        cache_ttl=ttl,
        jwks_uri=None if all(a.startswith("HS") for a in algs) else jwks_uri,
        hmac_secret_b64=hmac_secret_b64,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def jwks_source() -> FakeJwksSource:
    return FakeJwksSource()


@pytest.fixture
def registry(jwks_source: FakeJwksSource, clock: FakeClock) -> JwksCacheRegistry:
    return JwksCacheRegistry(fetcher=jwks_source, clock=clock)


@pytest.fixture
def rsa_key():
    return generate_key("RS256")


@pytest.fixture
def es256_key():
    return generate_key("ES256")


@pytest.fixture
def eddsa_key():
    return generate_key("EdDSA")


def standard_claims(
    *,
    iss: str = "https://issuer.example",
    aud: Any = "gateway-aud",
    now: float = 1_000_000.0,
    ttl: int = 300,
    nbf: float | None = None,
) -> dict[str, Any]:
    c: dict[str, Any] = {
        "iss": iss,
        "sub": "user-123",
        "aud": aud,
        "iat": int(now),
        "exp": int(now) + ttl,
        "jti": "token-1",
    }
    if nbf is not None:
        c["nbf"] = int(nbf)
    return c


def register_jwks(
    source: FakeJwksSource, cfg: IssuerConfig, keys: dict[str, Any]
) -> None:
    """keys: {kid: (private_key, alg|None)}。"""
    jwks = []
    for kid, (key, alg) in keys.items():
        jwks.append(public_jwk(key, kid, alg=alg))
    assert cfg.jwks_uri is not None
    source.set(cfg.jwks_uri, jwks_doc(*jwks))


@pytest.fixture
def make_env(
    clock: FakeClock,
    registry: JwksCacheRegistry,
    jwks_source: FakeJwksSource,
):
    """工厂：给定发行方配置列表 -> (app, client, verifier, source, clock)。"""
    from fastapi.testclient import TestClient

    def _make(issuers: list[IssuerConfig]):
        store = TrustStore(
            issuers={c.id: c for c in issuers},
            by_iss={c.iss: c for c in issuers},
        )
        app = create_app(store, registry=registry, clock=clock)
        client = TestClient(app)
        return app, client, store, jwks_source, clock

    return _make
