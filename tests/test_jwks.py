"""Unit tests for JWKS parsing and the HTTP fetcher."""

from __future__ import annotations

import json

import httpx
import pytest

from app.jwtv.jwks import (
    DuplicateKidError,
    HttpJWKSFetcher,
    JWKSFetchError,
    parse_jwks,
)

from .helpers import public_jwk, rsa_key, ec_key

ALLOWED = frozenset({"RS256", "ES256"})


def test_parse_valid_set():
    key = rsa_key()
    doc = json.dumps({"keys": [public_jwk(key, "k1", "RS256")]})
    parsed = parse_jwks(doc, ALLOWED)
    assert set(parsed) == {"k1"}
    assert parsed["k1"].alg == "RS256"


def test_duplicate_kid_raises():
    doc = json.dumps(
        {
            "keys": [
                public_jwk(rsa_key(), "k1", "RS256"),
                public_jwk(rsa_key(), "k1", "RS256"),
            ]
        }
    )
    with pytest.raises(DuplicateKidError):
        parse_jwks(doc, ALLOWED)


def test_disallowed_alg_key_is_invisible():
    key = rsa_key()
    jwk = public_jwk(key, "k1", "RS512")
    doc = json.dumps({"keys": [jwk]})
    assert parse_jwks(doc, ALLOWED) == {}


def test_symmetric_key_rejected():
    doc = json.dumps({"keys": [{"kty": "oct", "kid": "k1", "alg": "HS256", "k": "c2VjcmV0"}]})
    assert parse_jwks(doc, ALLOWED) == {}


def test_non_sig_use_key_skipped():
    jwk = public_jwk(rsa_key(), "k1", "RS256")
    jwk["use"] = "enc"
    doc = json.dumps({"keys": [jwk]})
    assert parse_jwks(doc, ALLOWED) == {}


def test_alg_must_match_key_type():
    jwk = public_jwk(ec_key(), "k1", "RS256")  # EC key labelled RS256
    doc = json.dumps({"keys": [jwk]})
    with pytest.raises(JWKSFetchError, match="does not match its key type"):
        parse_jwks(doc, ALLOWED)


def test_weak_rsa_key_rejected():
    from cryptography.hazmat.primitives.asymmetric import rsa

    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    doc = json.dumps({"keys": [public_jwk(weak, "k1", "RS256")]})
    with pytest.raises(JWKSFetchError, match="2048"):
        parse_jwks(doc, ALLOWED)


def test_not_a_jwks_document():
    with pytest.raises(JWKSFetchError):
        parse_jwks('{"keys": {}}', ALLOWED)
    with pytest.raises(JWKSFetchError):
        parse_jwks("not json", ALLOWED)


def test_non_finite_numbers_rejected():
    doc = '{"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256", "n": NaN, "e": "AQAB"}]}'
    with pytest.raises(JWKSFetchError):
        parse_jwks(doc, ALLOWED)


# ------------------------------------------------------------------- fetcher


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetcher_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"keys": []})

    fetcher = HttpJWKSFetcher(client=_client(handler))
    assert json.loads(await fetcher("https://idp.example/jwks.json")) == {"keys": []}


async def test_fetcher_rejects_redirect():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/jwks"})

    fetcher = HttpJWKSFetcher(client=_client(handler))
    with pytest.raises(JWKSFetchError, match="302"):
        await fetcher("https://idp.example/jwks.json")


async def test_fetcher_rejects_non_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    fetcher = HttpJWKSFetcher(client=_client(handler))
    with pytest.raises(JWKSFetchError, match="503"):
        await fetcher("https://idp.example/jwks.json")


async def test_fetcher_rejects_oversized_document():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"keys": [], "pad": "x" * 100_000})

    fetcher = HttpJWKSFetcher(client=_client(handler), max_bytes=1024)
    with pytest.raises(JWKSFetchError, match="size limit"):
        await fetcher("https://idp.example/jwks.json")


async def test_fetcher_rejects_non_json_content_type():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>", headers={"content-type": "text/html"})

    fetcher = HttpJWKSFetcher(client=_client(handler))
    with pytest.raises(JWKSFetchError, match="content-type"):
        await fetcher("https://idp.example/jwks.json")


async def test_fetcher_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    fetcher = HttpJWKSFetcher(client=_client(handler))
    with pytest.raises(JWKSFetchError, match="ConnectError"):
        await fetcher("https://idp.example/jwks.json")
