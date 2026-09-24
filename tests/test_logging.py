"""Logging safety: tokens never appear in log output."""

from __future__ import annotations

import logging

import pytest

from app.jwtv.errors import TokenError
from app.jwtv.jwks import IssuerRegistry
from app.jwtv.verifier import verify_token
from app.logging import audit_fields, configure_logging

from .helpers import (
    FakeClock,
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


def test_audit_fields_never_contain_token():
    fields = audit_fields(
        outcome="rejected",
        issuer_id="issuer-a",
        kid="k1",
        alg="RS256",
        fingerprint="sha256:0123456789abcdef",
        error_code="invalid_signature",
    )
    assert "token" not in fields
    assert "claims" not in fields
    assert fields["token_fingerprint"].startswith("sha256:")
    assert len(fields["token_fingerprint"]) <= 24


async def test_rejection_log_line_contains_no_token(caplog):
    clock = FakeClock(1000.0)
    private = rsa_key()
    server = FakeJwksServer()
    server.set_jwks(
        "https://issuer-a.example/a/jwks.json",
        [public_jwk(private, "k1", "RS256")],
    )
    config = issuer_config(issuer_id="issuer-a", iss=ISS_A, alg="RS256")
    registry = IssuerRegistry([config], server.fetch, clock=clock)

    # An HMAC-confusion token whose secret embeds a canary string; if any
    # log line leaked the token, the canary would show up.
    canary = "CANARY-SECRET-12345"
    token = make_hmac_token(canary.encode(), base_claims(ISS_A, AUD, clock.value))

    logger = configure_logging("DEBUG")
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(self.format(record))

    capture = Capture()
    capture.setFormatter(logger.handlers[0].formatter)
    logger.addHandler(capture)

    from app.jwtv.verifier import token_fingerprint

    fingerprint = token_fingerprint(token)
    try:
        await verify_token(token, registry, now=clock.value)
    except TokenError as exc:
        logger.warning(
            "token rejected",
            extra={
                "fields": audit_fields(
                    outcome="rejected",
                    issuer_id="issuer-a",
                    kid=None,
                    alg=None,
                    fingerprint=fingerprint,
                    error_code=exc.code,
                )
            },
        )

    joined = "\n".join(records)
    assert records, "expected at least one log record"
    assert token not in joined
    assert canary not in joined
    assert "CANARY" not in joined
    # The fingerprint (not the token) is what identifies it.
    assert fingerprint in joined
