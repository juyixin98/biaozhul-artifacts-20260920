"""End-to-end verifier tests: happy path, algorithm confusion, time
boundaries, key rotation, cache isolation, duplicate kids."""

from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives import serialization

from app.jwtv.errors import TokenError
from app.jwtv.jwks import IssuerRegistry
from app.jwtv.verifier import verify_token

from .helpers import (
    FakeClock,
    FakeJwksServer,
    base_claims,
    ec_key,
    issuer_config,
    make_hmac_token,
    make_token,
    make_unsigned_none_token,
    public_jwk,
    rsa_key,
)

ISS_A = "https://issuer-a.example/"
ISS_B = "https://issuer-b.example/"
AUD = "test-audience"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(1000.0)


@pytest.fixture
def rsa_private():
    return rsa_key()


@pytest.fixture
def ec_private():
    return ec_key()


@pytest.fixture
def server(rsa_private, ec_private) -> FakeJwksServer:
    srv = FakeJwksServer()
    srv.set_jwks(
        "https://issuer-a.example/a/jwks.json",
        [public_jwk(rsa_private, "k1", "RS256")],
    )
    srv.set_jwks(
        "https://issuer-b.example/b/jwks.json",
        [public_jwk(ec_private, "k1", "ES256")],
    )
    return srv


@pytest.fixture
def registry(server, clock) -> IssuerRegistry:
    configs = [
        issuer_config(issuer_id="issuer-a", iss=ISS_A, alg="RS256"),
        issuer_config(
            issuer_id="issuer-b",
            iss=ISS_B,
            alg="ES256",
            jwks_path="/b/jwks.json",
            base="https://issuer-b.example",
        ),
    ]
    return IssuerRegistry(configs, server.fetch, clock=clock)


async def expect_error(token, registry, code, *, now=None, issuer_id=None):
    with pytest.raises(TokenError) as excinfo:
        await verify_token(token, registry, explicit_issuer_id=issuer_id, now=now)
    assert excinfo.value.code == code
    return excinfo.value


# ---------------------------------------------------------------- happy path


async def test_valid_rs256_token_accepted(registry, rsa_private, clock):
    token = make_token(rsa_private, claims=base_claims(ISS_A, AUD, clock.value))
    result = await verify_token(token, registry, now=clock.value)
    assert result.issuer_id == "issuer-a"
    assert result.alg == "RS256"
    assert result.kid == "k1"
    assert result.claims["sub"] == "subject-1"


async def test_valid_es256_token_accepted(registry, ec_private, clock):
    token = make_token(
        ec_private, claims=base_claims(ISS_B, AUD, clock.value), alg="ES256"
    )
    result = await verify_token(token, registry, now=clock.value)
    assert result.issuer_id == "issuer-b"
    assert result.alg == "ES256"


async def test_explicit_issuer_id_selects_issuer(registry, rsa_private, clock):
    token = make_token(rsa_private, claims=base_claims(ISS_A, AUD, clock.value))
    result = await verify_token(token, registry, explicit_issuer_id="issuer-a", now=clock.value)
    assert result.issuer_id == "issuer-a"


# ------------------------------------------------------- algorithm confusion


async def test_alg_none_rejected(registry, clock):
    token = make_unsigned_none_token(base_claims(ISS_A, AUD, clock.value))
    await expect_error(token, registry, "algorithm_not_allowed", now=clock.value)


async def test_rs256_to_hs256_confusion_rejected(registry, rsa_private, clock):
    """The classic attack: reuse the issuer's kid, flip alg to HS256, and
    sign with the RSA public key bytes as the HMAC secret. The gateway must
    refuse because HS256 is not in the issuer allow-list — before any key
    material is ever used as an HMAC secret."""
    public_der = rsa_private.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    token = make_hmac_token(public_der, base_claims(ISS_A, AUD, clock.value), kid="k1")
    err = await expect_error(token, registry, "algorithm_not_allowed", now=clock.value)
    assert err.details["alg"] == "HS256"


async def test_es256_token_against_rs256_issuer_rejected(registry, ec_private, clock):
    """Issuer A only allows RS256; an ES256 token claiming issuer A is refused."""
    token = make_token(
        ec_private, claims=base_claims(ISS_A, AUD, clock.value), alg="ES256"
    )
    await expect_error(token, registry, "algorithm_not_allowed", now=clock.value)


async def test_jku_header_rejected(registry, rsa_private, clock):
    token = make_token(
        rsa_private,
        claims=base_claims(ISS_A, AUD, clock.value),
        header_extra={"jku": "https://attacker.example/jwks.json"},
    )
    err = await expect_error(
        token, registry, "header_key_reference_forbidden", now=clock.value
    )
    assert err.details["parameter"] == "jku"


async def test_x5u_header_rejected(registry, rsa_private, clock):
    token = make_token(
        rsa_private,
        claims=base_claims(ISS_A, AUD, clock.value),
        header_extra={"x5u": "https://attacker.example/cert.pem"},
    )
    await expect_error(token, registry, "header_key_reference_forbidden", now=clock.value)


async def test_embedded_jwk_header_rejected(registry, rsa_private, clock):
    token = make_token(
        rsa_private,
        claims=base_claims(ISS_A, AUD, clock.value),
        header_extra={"jwk": {"kty": "RSA", "n": "x", "e": "AQAB"}},
    )
    await expect_error(token, registry, "header_key_reference_forbidden", now=clock.value)


async def test_crit_header_rejected(registry, rsa_private, clock):
    token = make_token(
        rsa_private,
        claims=base_claims(ISS_A, AUD, clock.value),
        header_extra={"crit": ["exp"], "exp": "anything"},
    )
    await expect_error(token, registry, "critical_header_unsupported", now=clock.value)


async def test_missing_kid_rejected(registry, rsa_private, clock):
    header = {"alg": "RS256", "typ": "JWT"}
    from .helpers import encode_json, b64
    from app.jwtv.jws import sign

    h = encode_json(header)
    p = encode_json(base_claims(ISS_A, AUD, clock.value))
    sig = b64(sign("RS256", rsa_private, f"{h}.{p}".encode()))
    await expect_error(f"{h}.{p}.{sig}", registry, "missing_kid", now=clock.value)


# ------------------------------------------------------------- time boundary


async def test_exp_exact_boundary_accepted(registry, rsa_private, clock):
    claims = base_claims(ISS_A, AUD, clock.value)
    claims["exp"] = int(clock.value)  # exp == now is still valid
    token = make_token(rsa_private, claims=claims)
    result = await verify_token(token, registry, now=clock.value)
    assert result.issuer_id == "issuer-a"


async def test_exp_one_second_past_rejected(registry, rsa_private, clock):
    claims = base_claims(ISS_A, AUD, clock.value)
    claims["exp"] = int(clock.value) - 1
    token = make_token(rsa_private, claims=claims)
    await expect_error(token, registry, "token_expired", now=clock.value)


async def test_nbf_exact_boundary_accepted(registry, rsa_private, clock):
    claims = base_claims(ISS_A, AUD, clock.value)
    claims["nbf"] = int(clock.value)
    token = make_token(rsa_private, claims=claims)
    result = await verify_token(token, registry, now=clock.value)
    assert result.issuer_id == "issuer-a"


async def test_nbf_one_second_future_rejected(registry, rsa_private, clock):
    claims = base_claims(ISS_A, AUD, clock.value)
    claims["nbf"] = int(clock.value) + 1
    token = make_token(rsa_private, claims=claims)
    await expect_error(token, registry, "token_not_yet_valid", now=clock.value)


async def test_leeway_covers_skew(server, clock, rsa_private):
    configs = [
        issuer_config(issuer_id="issuer-a", iss=ISS_A, alg="RS256", leeway=60),
    ]
    registry = IssuerRegistry(configs, server.fetch, clock=clock)
    claims = base_claims(ISS_A, AUD, clock.value)
    claims["exp"] = int(clock.value) - 30  # 30s expired, within 60s leeway
    token = make_token(rsa_private, claims=claims)
    result = await verify_token(token, registry, now=clock.value)
    assert result.issuer_id == "issuer-a"


async def test_iat_in_future_rejected(registry, rsa_private, clock):
    claims = base_claims(ISS_A, AUD, clock.value)
    claims["iat"] = int(clock.value) + 600
    token = make_token(rsa_private, claims=claims)
    await expect_error(token, registry, "issued_in_future", now=clock.value)


async def test_non_numeric_exp_rejected(registry, rsa_private, clock):
    claims = base_claims(ISS_A, AUD, clock.value)
    claims["exp"] = "tomorrow"
    token = make_token(rsa_private, claims=claims)
    await expect_error(token, registry, "invalid_claim", now=clock.value)


# ------------------------------------------------------------------- claims


async def test_wrong_audience_rejected(registry, rsa_private, clock):
    claims = base_claims(ISS_A, "other-audience", clock.value)
    token = make_token(rsa_private, claims=claims)
    await expect_error(token, registry, "invalid_audience", now=clock.value)


async def test_audience_list_accepted(registry, rsa_private, clock):
    claims = base_claims(ISS_A, AUD, clock.value)
    claims["aud"] = ["unrelated", AUD]
    token = make_token(rsa_private, claims=claims)
    result = await verify_token(token, registry, now=clock.value)
    assert result.issuer_id == "issuer-a"


async def test_iss_mismatch_after_signature_rejected(registry, rsa_private, clock):
    """Token routes to issuer A via an explicit issuer_id, but the 'iss'
    claim inside the signed payload disagrees with the configured value."""
    claims = base_claims(ISS_A, AUD, clock.value)
    token = make_token(rsa_private, claims=claims)
    from app.jwtv.jwks import IssuerRegistry as Reg

    cfg = issuer_config(issuer_id="issuer-a", iss="https://different.example/")
    server = FakeJwksServer()
    server.set_jwks(cfg.jwks_uri, [public_jwk(rsa_private, "k1", "RS256")])
    reg = Reg([cfg], server.fetch, clock=clock)
    await expect_error(
        token, reg, "issuer_mismatch", now=clock.value, issuer_id="issuer-a"
    )


async def test_unknown_issuer_rejected(registry, rsa_private, clock):
    claims = base_claims("https://untrusted.example/", AUD, clock.value)
    token = make_token(rsa_private, claims=claims)
    await expect_error(token, registry, "unknown_issuer", now=clock.value)


async def test_missing_iss_claim_rejected(registry, rsa_private, clock):
    claims = base_claims(ISS_A, AUD, clock.value)
    del claims["iss"]
    token = make_token(rsa_private, claims=claims)
    await expect_error(token, registry, "missing_issuer", now=clock.value)


async def test_explicit_unknown_issuer_id_rejected(registry, rsa_private, clock):
    token = make_token(rsa_private, claims=base_claims(ISS_A, AUD, clock.value))
    err = await expect_error(
        token, registry, "unknown_issuer", now=clock.value, issuer_id="nope"
    )
    assert err.status_code == 400


# -------------------------------------------------------------- signatures


async def test_tampered_payload_rejected(registry, rsa_private, clock):
    token = make_token(rsa_private, claims=base_claims(ISS_A, AUD, clock.value))
    h, p, s = token.split(".")
    tampered = make_token(
        rsa_private,
        claims={**base_claims(ISS_A, AUD, clock.value), "admin": True},
    )
    h2, p2, _ = tampered.split(".")
    # original signature, modified payload
    await expect_error(f"{h2}.{p2}.{s}", registry, "invalid_signature", now=clock.value)


async def test_signature_from_other_key_rejected(registry, clock):
    attacker = rsa_key()
    token = make_token(attacker, claims=base_claims(ISS_A, AUD, clock.value), kid="k1")
    await expect_error(token, registry, "invalid_signature", now=clock.value)


async def test_empty_signature_rejected(registry, rsa_private, clock):
    token = make_token(
        rsa_private,
        claims=base_claims(ISS_A, AUD, clock.value),
        unsigned_signature="",
    )
    await expect_error(token, registry, "empty_signature", now=clock.value)


async def test_malformed_token_rejected(registry):
    await expect_error("not-a-jwt", registry, "malformed_token")
    await expect_error("a.b.c.d", registry, "malformed_token")
    await expect_error("a.!invalid!.c", registry, "malformed_token")


async def test_missing_token_rejected(registry):
    await expect_error("", registry, "missing_token")


# ------------------------------------------------------------- key rotation


async def test_unknown_kid_triggers_single_refresh_and_succeeds(
    server, clock, rsa_private
):
    """Rotation: issuer publishes a new kid. The first token with the new
    kid triggers exactly one JWKS refresh and then verifies."""
    new_key = rsa_key()
    config = issuer_config(issuer_id="issuer-a", iss=ISS_A, alg="RS256")
    registry = IssuerRegistry([config], server.fetch, clock=clock)

    # Warm the cache with the old key.
    old_token = make_token(rsa_private, claims=base_claims(ISS_A, AUD, clock.value))
    await verify_token(old_token, registry, now=clock.value)
    assert server.fetch_counts[config.jwks_uri] == 1

    # Issuer rotates: JWKS now serves only the new key.
    server.set_jwks(config.jwks_uri, [public_jwk(new_key, "k2", "RS256")])
    new_token = make_token(
        new_key, claims=base_claims(ISS_A, AUD, clock.value), kid="k2"
    )
    result = await verify_token(new_token, registry, now=clock.value)
    assert result.kid == "k2"
    assert server.fetch_counts[config.jwks_uri] == 2  # exactly one extra fetch


async def test_unknown_kid_negative_cooldown(server, clock, rsa_private):
    """Repeated unknown kids must not cause a fetch storm."""
    config = issuer_config(
        issuer_id="issuer-a", iss=ISS_A, alg="RS256", cooldown=30
    )
    registry = IssuerRegistry([config], server.fetch, clock=clock)
    token = make_token(
        rsa_private, claims=base_claims(ISS_A, AUD, clock.value), kid="ghost"
    )
    await expect_error(token, registry, "unknown_kid", now=clock.value)
    await expect_error(token, registry, "unknown_kid", now=clock.value)
    await expect_error(token, registry, "unknown_kid", now=clock.value)
    assert server.fetch_counts[config.jwks_uri] == 1  # cooldown suppressed refetches

    clock.advance(31)  # cooldown elapsed -> one more fetch allowed
    await expect_error(token, registry, "unknown_kid", now=clock.value)
    assert server.fetch_counts[config.jwks_uri] == 2


async def test_ttl_expiry_triggers_refresh(server, clock, rsa_private):
    config = issuer_config(
        issuer_id="issuer-a", iss=ISS_A, alg="RS256", ttl=100
    )
    registry = IssuerRegistry([config], server.fetch, clock=clock)
    token = make_token(rsa_private, claims=base_claims(ISS_A, AUD, clock.value))
    await verify_token(token, registry, now=clock.value)
    assert server.fetch_counts[config.jwks_uri] == 1

    clock.advance(99)  # within TTL -> cache hit
    await verify_token(token, registry, now=clock.value)
    assert server.fetch_counts[config.jwks_uri] == 1

    clock.advance(2)  # past TTL -> refresh
    await verify_token(token, registry, now=clock.value)
    assert server.fetch_counts[config.jwks_uri] == 2


async def test_rotation_within_ttl_via_admin_refresh(server, clock, rsa_private):
    """Keys rotate while the cache is still fresh: admin refresh picks the
    new key up without waiting for TTL."""
    config = issuer_config(issuer_id="issuer-a", iss=ISS_A, alg="RS256", ttl=3600)
    registry = IssuerRegistry([config], server.fetch, clock=clock)
    token = make_token(rsa_private, claims=base_claims(ISS_A, AUD, clock.value))
    await verify_token(token, registry, now=clock.value)

    new_key = rsa_key()
    server.set_jwks(config.jwks_uri, [public_jwk(new_key, "k2", "RS256")])
    cache = registry.cache_by_id("issuer-a")
    await cache.force_refresh()

    new_token = make_token(
        new_key, claims=base_claims(ISS_A, AUD, clock.value), kid="k2"
    )
    result = await verify_token(new_token, registry, now=clock.value)
    assert result.kid == "k2"
    # Old key is gone from the set: old tokens now fail.
    await expect_error(token, registry, "unknown_kid", now=clock.value)


async def test_failed_refresh_keeps_previous_keys(server, clock, rsa_private):
    config = issuer_config(issuer_id="issuer-a", iss=ISS_A, alg="RS256", ttl=100)
    registry = IssuerRegistry([config], server.fetch, clock=clock)
    token = make_token(rsa_private, claims=base_claims(ISS_A, AUD, clock.value))
    await verify_token(token, registry, now=clock.value)

    server.fail_urls.add(config.jwks_uri)
    clock.advance(200)  # TTL expired, refresh will fail
    result = await verify_token(token, registry, now=clock.value)
    assert result.issuer_id == "issuer-a"  # stale-but-good keys still serve


# ------------------------------------------------------------ cache isolation


async def test_same_kid_different_issuers_isolated(registry, rsa_private, ec_private, clock):
    """Both issuers publish a key with kid 'k1'. Each token must verify
    against its own issuer's key only."""
    token_a = make_token(rsa_private, claims=base_claims(ISS_A, AUD, clock.value))
    token_b = make_token(
        ec_private, claims=base_claims(ISS_B, AUD, clock.value), alg="ES256"
    )
    assert (await verify_token(token_a, registry, now=clock.value)).issuer_id == "issuer-a"
    assert (await verify_token(token_b, registry, now=clock.value)).issuer_id == "issuer-b"

    # Cross-issuer replay: issuer A's RS256 token pinned to issuer-b fails
    # issuer-b's algorithm allow-list (ES256 only).
    await expect_error(
        token_a, registry, "algorithm_not_allowed", now=clock.value, issuer_id="issuer-b"
    )


async def test_issuer_b_cache_untouched_by_issuer_a_traffic(
    registry, server, rsa_private, clock
):
    token_a = make_token(rsa_private, claims=base_claims(ISS_A, AUD, clock.value))
    await verify_token(token_a, registry, now=clock.value)
    assert server.fetch_counts.get("https://issuer-b.example/b/jwks.json", 0) == 0


# -------------------------------------------------------------- duplicate kid


async def test_duplicate_kid_in_jwks_rejected(clock, rsa_private):
    config = issuer_config(issuer_id="issuer-a", iss=ISS_A, alg="RS256")
    server = FakeJwksServer()
    other = rsa_key()
    server.set_jwks(
        config.jwks_uri,
        [
            public_jwk(rsa_private, "k1", "RS256"),
            public_jwk(other, "k1", "RS256"),  # same kid, different key
        ],
    )
    registry = IssuerRegistry([config], server.fetch, clock=clock)
    token = make_token(rsa_private, claims=base_claims(ISS_A, AUD, clock.value))
    err = await expect_error(token, registry, "duplicate_kid", now=clock.value)
    assert "k1" in err.details["reason"]


async def test_duplicate_kid_only_among_usable_keys(clock, rsa_private):
    """A second key with the same kid but a disallowed alg is invisible, so
    the set is still usable (it cannot be selected for verification)."""
    config = issuer_config(issuer_id="issuer-a", iss=ISS_A, alg="RS256")
    server = FakeJwksServer()
    dup = public_jwk(rsa_key(), "k1", "RS512")  # RS512 not allowed for issuer
    server.set_jwks(config.jwks_uri, [public_jwk(rsa_private, "k1", "RS256"), dup])
    registry = IssuerRegistry([config], server.fetch, clock=clock)
    token = make_token(rsa_private, claims=base_claims(ISS_A, AUD, clock.value))
    result = await verify_token(token, registry, now=clock.value)
    assert result.issuer_id == "issuer-a"


# --------------------------------------------------------------- jwks errors


async def test_jwks_fetch_failure_cold_cache(server, clock, rsa_private):
    config = issuer_config(issuer_id="issuer-a", iss=ISS_A, alg="RS256")
    server.fail_urls.add(config.jwks_uri)
    registry = IssuerRegistry([config], server.fetch, clock=clock)
    token = make_token(rsa_private, claims=base_claims(ISS_A, AUD, clock.value))
    await expect_error(token, registry, "jwks_unavailable", now=clock.value)


async def test_jwks_invalid_json(clock, rsa_private):
    config = issuer_config(issuer_id="issuer-a", iss=ISS_A, alg="RS256")
    server = FakeJwksServer()
    server.documents[config.jwks_uri] = {"keys": "not-a-list"}
    registry = IssuerRegistry([config], server.fetch, clock=clock)
    token = make_token(rsa_private, claims=base_claims(ISS_A, AUD, clock.value))
    await expect_error(token, registry, "jwks_unavailable", now=clock.value)


async def test_key_alg_mismatch(clock, rsa_private):
    """JWK declares RS384 but token header says RS256."""
    config = issuer_config(
        issuer_id="issuer-a", iss=ISS_A, alg="RS256", algorithms=["RS256", "RS384"]
    )
    server = FakeJwksServer()
    server.set_jwks(config.jwks_uri, [public_jwk(rsa_private, "k1", "RS384")])
    registry = IssuerRegistry([config], server.fetch, clock=clock)
    token = make_token(rsa_private, claims=base_claims(ISS_A, AUD, clock.value))
    await expect_error(token, registry, "key_alg_mismatch", now=clock.value)
