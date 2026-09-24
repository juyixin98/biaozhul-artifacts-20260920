"""Tests for the real cryptographic operations (HMAC-SHA256 tokens)."""
from __future__ import annotations

import pytest

from app.security import (
    TokenError,
    new_secret,
    sha256_fingerprint,
    sign_token,
    verify_token,
)


def test_secret_is_random_and_long_enough():
    a, b = new_secret(), new_secret()
    assert a != b
    assert len(bytes.fromhex(a)) >= 32  # >= 256 bits


def test_signed_token_roundtrip():
    secret = new_secret()
    payload = {"drain": "d-1", "step": 2, "egen": 7}
    token = sign_token(secret, payload)
    assert verify_token(secret, token) == payload
    # Token has exactly two parts (body, signature).
    assert token.count(".") == 1


def test_wrong_secret_is_rejected():
    with pytest.raises(TokenError):
        verify_token(new_secret(), sign_token(new_secret(), {"a": 1}))


@pytest.mark.parametrize("tamper", [
    lambda b, s: (b[:-2] + ("AA" if not b.endswith("AA") else "BB"), s),
    lambda b, s: (b, s[:-2] + ("00" if not s.endswith("00") else "11")),
    lambda b, s: (b + "eA", s),
])
def test_tampering_is_always_detected(tamper):
    secret = new_secret()
    body, sig = sign_token(secret, {"drain": "d"}).split(".")
    bad_body, bad_sig = tamper(body, sig)
    with pytest.raises(TokenError):
        verify_token(secret, f"{bad_body}.{bad_sig}")


def test_signature_is_deterministic_over_payload():
    secret = new_secret()
    payload = {"x": 1}
    assert sign_token(secret, payload) == sign_token(secret, dict(payload))


def test_different_payload_different_token():
    secret = new_secret()
    assert sign_token(secret, {"step": 1}) != sign_token(secret, {"step": 2})


def test_fingerprint_is_stable_order_independent():
    a = {"z": 1, "a": [1, 2]}
    b = {"a": [1, 2], "z": 1}
    assert sha256_fingerprint(a) == sha256_fingerprint(b)
    c = {"z": 1, "a": [2, 1]}
    assert sha256_fingerprint(a) != sha256_fingerprint(c)


def test_malformed_tokens_rejected():
    secret = new_secret()
    for bad in ["", "nodot", "a.b.c", ".", "!!!.@@@"]:
        with pytest.raises(TokenError):
            verify_token(secret, bad)
