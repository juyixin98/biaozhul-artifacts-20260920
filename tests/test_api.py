"""HTTP API tests: endpoints, structured error envelope, header redaction."""

from __future__ import annotations

import time

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import create_app

from .helpers import (
    FakeJwksServer,
    base_claims,
    issuer_config,
    make_hmac_token,
    make_token,
    public_jwk,
    rsa_key,
)

ISS_A = "https://issuer-a.example/"
AUD = "test-audience"
NOW = time.time()


@pytest.fixture
def private_key():
    return rsa_key()


@pytest.fixture
def server(private_key) -> FakeJwksServer:
    srv = FakeJwksServer()
    srv.set_jwks(
        "https://issuer-a.example/a/jwks.json",
        [public_jwk(private_key, "k1", "RS256")],
    )
    return srv


@pytest.fixture
def configs():
    return [issuer_config(issuer_id="issuer-a", iss=ISS_A, alg="RS256")]


@pytest_asyncio.fixture
async def client(configs, server):
    app = create_app(configs, fetcher=server.fetch, log_level="WARNING")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c


async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_list_issuers(client):
    resp = await client.get("/v1/issuers")
    assert resp.status_code == 200
    issuers = resp.json()["issuers"]
    assert issuers[0]["issuer_id"] == "issuer-a"
    assert issuers[0]["allowed_algorithms"] == ["RS256"]
    assert "jwks_uri" not in issuers[0]  # internal detail, not exposed


async def test_verify_valid_token_json_body(client, private_key):
    token = make_token(private_key, claims=base_claims(ISS_A, AUD, NOW))
    resp = await client.post("/v1/verify", json={"token": token})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["issuer_id"] == "issuer-a"
    assert body["claims"]["sub"] == "subject-1"


async def test_verify_valid_token_bearer_header(client, private_key):
    token = make_token(private_key, claims=base_claims(ISS_A, AUD, NOW))
    resp = await client.post(
        "/v1/verify", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_verify_rejection_envelope(client, private_key):
    claims = base_claims(ISS_A, "wrong-audience", NOW)
    token = make_token(private_key, claims=claims)
    resp = await client.post("/v1/verify", json={"token": token})
    assert resp.status_code == 401
    body = resp.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "invalid_audience"
    assert resp.headers["www-authenticate"].startswith("Bearer error=")
    # The token itself must not appear anywhere in the response.
    assert token not in resp.text


async def test_alg_confusion_over_http(client):
    token = make_hmac_token(b"secret", base_claims(ISS_A, AUD, NOW))
    resp = await client.post("/v1/verify", json={"token": token})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "algorithm_not_allowed"


async def test_missing_token(client):
    resp = await client.post("/v1/verify", json={})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] in ("missing_token", "invalid_request")


async def test_unknown_issuer_id_param(client, private_key):
    token = make_token(private_key, claims=base_claims(ISS_A, AUD, NOW))
    resp = await client.post(
        "/v1/verify", json={"token": token, "issuer_id": "ghost"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unknown_issuer"


async def test_admin_cache_endpoints(client, private_key):
    token = make_token(private_key, claims=base_claims(ISS_A, AUD, NOW))
    await client.post("/v1/verify", json={"token": token})

    resp = await client.get("/admin/issuers/issuer-a/cache")
    assert resp.status_code == 200
    snap = resp.json()
    assert snap["issuer_id"] == "issuer-a"
    assert "k1" in snap["keys"]
    assert snap["keys"]["k1"]["alg"] == "RS256"

    resp = await client.post("/admin/issuers/issuer-a/refresh")
    assert resp.status_code == 200
    assert resp.json()["last_error"] is None

    resp = await client.get("/admin/issuers/ghost/cache")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "unknown_issuer"
