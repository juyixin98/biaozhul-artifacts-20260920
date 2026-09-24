"""Unit tests for HMAC authorization tokens and mapping-epoch invalidation."""

import time

import pytest

from mr_gateway import auth
from mr_gateway.auth import (
    Claims, TokenExpired, TokenMalformed, TokenSignatureInvalid, TokenStale,
)

SECRET = "unit-hmac-secret"


def test_token_roundtrip():
    tok = auth.issue_token(SECRET, "alpha", "tester-1", epoch=1,
                           ttl_seconds=60)
    claims = auth.verify_token(tok, SECRET, "alpha", 1)
    assert claims == Claims("alpha", "tester-1", 1, claims.expires_at)


def test_token_is_compact_three_part_string():
    tok = auth.issue_token(SECRET, "alpha", "tester-1", 1, 60)
    parts = tok.split(".")
    assert parts[0] == "mr1" and len(parts) == 3


def test_signature_rejects_tampered_payload_and_foreign_secret():
    tok = auth.issue_token(SECRET, "alpha", "tester-1", 1, 60)
    kid, payload, sig = tok.split(".")
    # flip a payload character
    tampered_payload = ("A" if payload[0] != "A" else "B") + payload[1:]
    with pytest.raises(TokenSignatureInvalid):
        auth.verify_token(f"{kid}.{tampered_payload}.{sig}", SECRET,
                          "alpha", 1)
    with pytest.raises(TokenSignatureInvalid):
        auth.verify_token(tok, "another-secret", "alpha", 1)


def test_wrong_robot_binding_rejected():
    tok = auth.issue_token(SECRET, "alpha", "tester-1", 1, 60)
    with pytest.raises(TokenSignatureInvalid):
        auth.verify_token(tok, SECRET, "beta", 1)


def test_expired_token_rejected():
    now = 1_000_000.0
    tok = auth.issue_token(SECRET, "alpha", "tester-1", 1, 10, now=now)
    with pytest.raises(TokenExpired):
        auth.verify_token(tok, SECRET, "alpha", 1, now=now + 11)
    # still valid at the instant before expiry
    auth.verify_token(tok, SECRET, "alpha", 1, now=now + 9)


def test_epoch_bump_stales_token():
    # Mapping changes: registry would bump epoch 1 -> 2. The old token must
    # immediately fail with TokenStale.
    tok = auth.issue_token(SECRET, "alpha", "tester-1", epoch=1,
                           ttl_seconds=3600)
    auth.verify_token(tok, SECRET, "alpha", 1)  # still good on epoch 1
    with pytest.raises(TokenStale):
        auth.verify_token(tok, SECRET, "alpha", 2)
    # a new token at epoch 2 works
    tok2 = auth.issue_token(SECRET, "alpha", "tester-1", epoch=2,
                            ttl_seconds=3600)
    auth.verify_token(tok2, SECRET, "alpha", 2)


@pytest.mark.parametrize("garbage", [
    "", "x", "mr1.ab", "mr2.ab.cd", "mr1.!!!.sig", "Bearer mr1.ab.cd",
    "mr1.bm90.fake",
])
def test_malformed_tokens(garbage):
    with pytest.raises(auth.TokenError):
        auth.verify_token(garbage, SECRET, "alpha", 1)
