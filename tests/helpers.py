"""Shared test helpers: keys, token minting, fake clock, in-memory JWKS server."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app.jwtv.b64 import b64url_encode
from app.jwtv.jwk import public_jwk_from_private_key
from app.jwtv.jws import sign
from app.jwtv.jwks import IssuerConfig


def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def ec_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def private_for(alg: str):
    if alg.startswith("RS"):
        return rsa_key()
    if alg.startswith("ES"):
        return ec_key()
    raise ValueError(alg)


def b64(data: bytes) -> str:
    return b64url_encode(data)


def encode_json(obj: Any) -> str:
    return b64(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def make_token(
    signer: Any,
    *,
    claims: dict[str, Any],
    alg: str = "RS256",
    kid: str = "k1",
    header_extra: Optional[dict[str, Any]] = None,
    unsigned_signature: Optional[str] = None,
    mutate_signature: bool = False,
) -> str:
    header: dict[str, Any] = {"alg": alg, "typ": "JWT", "kid": kid}
    if header_extra:
        header.update(header_extra)
    h = encode_json(header)
    p = encode_json(claims)
    if unsigned_signature is not None:
        return f"{h}.{p}.{unsigned_signature}"
    sig = b64(sign(alg, signer, f"{h}.{p}".encode("utf-8")))
    if mutate_signature:
        sig = "A" + sig[1:] if len(sig) > 1 else "A"
    return f"{h}.{p}.{sig}"


def make_unsigned_none_token(claims: dict[str, Any]) -> str:
    h = encode_json({"alg": "none", "typ": "JWT"})
    p = encode_json(claims)
    return f"{h}.{p}."


def make_hmac_token(secret: bytes, claims: dict[str, Any], *, kid: str = "k1") -> str:
    header = {"alg": "HS256", "typ": "JWT", "kid": kid}
    h = encode_json(header)
    p = encode_json(claims)
    sig = b64(sign("HS256", secret, f"{h}.{p}".encode("utf-8")))
    return f"{h}.{p}.{sig}"


def public_jwk(private_key: Any, kid: str, alg: str) -> dict[str, Any]:
    return public_jwk_from_private_key(private_key, kid, alg)


class FakeClock:
    """Adjustable monotonic-style clock shared between cache and verifier."""

    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> float:
        self.value += seconds
        return self.value


@dataclass
class FakeJwksServer:
    """In-process JWKS source behind a simple async fetcher.

    ``documents`` maps issuer jwks_uri -> dict (served as JSON).
    """

    documents: dict[str, dict[str, Any]] = field(default_factory=dict)
    fetch_counts: dict[str, int] = field(default_factory=dict)
    fail_urls: set[str] = field(default_factory=set)
    fail_once: set[str] = field(default_factory=set)

    async def fetch(self, url: str) -> str:
        self.fetch_counts[url] = self.fetch_counts.get(url, 0) + 1
        if url in self.fail_urls or url in self.fail_once:
            self.fail_once.discard(url)
            from app.jwtv.jwks import JWKSFetchError

            raise JWKSFetchError(f"simulated failure fetching {url}")
        if url not in self.documents:
            from app.jwtv.jwks import JWKSFetchError

            raise JWKSFetchError(f"no document at {url}")
        return json.dumps(self.documents[url])

    def set_jwks(self, url: str, keys: list[dict[str, Any]]) -> None:
        self.documents[url] = {"keys": keys}


def issuer_config(
    *,
    issuer_id: str = "issuer-a",
    iss: str = "https://issuer-a.example/",
    alg: str = "RS256",
    algorithms: Optional[list[str]] = None,
    audience: str = "test-audience",
    jwks_path: str = "/a/jwks.json",
    ttl: int = 600,
    cooldown: int = 30,
    leeway: int = 0,
    base: str = "https://issuer-a.example",
) -> IssuerConfig:
    return IssuerConfig(
        issuer_id=issuer_id,
        iss=iss,
        jwks_uri=base + jwks_path,
        audience=audience,
        algorithms=frozenset(algorithms or [alg]),
        cache_ttl_seconds=ttl,
        negative_cooldown_seconds=cooldown,
        leeway_seconds=leeway,
    )


def base_claims(iss: str, audience: str, now: float) -> dict[str, Any]:
    t = int(now)
    return {
        "iss": iss,
        "sub": "subject-1",
        "aud": audience,
        "iat": t,
        "nbf": t - 10,
        "exp": t + 3600,
        "jti": "jti-1",
    }
