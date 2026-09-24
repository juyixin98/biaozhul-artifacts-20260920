"""The token verification pipeline.

Order of operations is deliberate:

1. Structural parsing — three base64url segments, JSON header/payload that
   are JSON objects. Nothing in the token is trusted yet.
2. Header policy — the algorithm must be explicitly allowed for this
   issuer; ``jku``/``x5u``/``jwk`` and the ``none`` algorithm family are
   refused, so a token can never choose its own keys or verifier URL.
3. Issuer routing from the (unverified) ``iss`` claim — used only to find
   the right local configuration; the claim itself is re-checked after
   signature verification.
4. Signature verification with the JWKS key pinned to the allow-listed
   algorithm family.
5. Only then: iss / aud / exp / nbf / iat claim checks.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import claims as claim_checks
from .b64 import b64url_decode
from .errors import TokenError, reject
from .jws import verify_signature
from .jwks import IssuerCache, IssuerConfig, IssuerRegistry

Clock = Callable[[], float]
wall_clock: Clock = time.time

#: JOSE/JWT header parameters that can steer key resolution or algorithm
#: selection; a verification gateway must not honour any of them from a token.
_FORBIDDEN_HEADER_PARAMS = ("jku", "jwk", "x5u", "x5c", "x5t", "x5t#S256")


@dataclass(frozen=True)
class VerifiedToken:
    issuer_id: str
    iss: str
    kid: str
    alg: str
    claims: dict[str, Any]
    fingerprint: str


def token_fingerprint(token: str) -> str:
    """Short non-reversible digest used in logs instead of the token."""
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _json_object(raw: bytes, code: str, message: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise reject(code, message)
    if not isinstance(value, dict):
        raise reject(code, message)
    return value


def _split(token: str) -> tuple[bytes, bytes, bytes, bytes]:
    if not isinstance(token, str) or not token.strip():
        raise reject("missing_token", "no token supplied", status_code=400)
    token = token.strip()
    parts = token.split(".")
    if len(parts) != 3:
        raise reject(
            "malformed_token",
            "JWT must have exactly three dot-separated segments",
            segments=len(parts),
        )
    header_b64, payload_b64, signature_b64 = parts
    try:
        header_raw = b64url_decode(header_b64)
        payload_raw = b64url_decode(payload_b64)
        # An empty signature segment is legal base64url-wise (alg=none
        # tokens); it is rejected later, after header policy has run.
        signature = b64url_decode(signature_b64) if signature_b64 else b""
    except ValueError as exc:
        raise reject("malformed_token", f"segment is not valid base64url: {exc}")
    signing_input = (header_b64 + "." + payload_b64).encode("ascii")
    return header_raw, payload_raw, signature, signing_input


def _select_issuer(
    header: dict[str, Any],
    payload: dict[str, Any],
    registry: IssuerRegistry,
    explicit_issuer_id: Optional[str],
) -> IssuerConfig:
    if explicit_issuer_id is not None:
        config = registry.get(explicit_issuer_id)
        if config is None:
            raise reject(
                "unknown_issuer",
                "issuer is not configured on this gateway",
                issuer_id=explicit_issuer_id,
                status_code=400,
            )
        return config

    iss = payload.get("iss")
    if not isinstance(iss, str) or not iss:
        raise reject(
            "missing_issuer",
            "token has no usable 'iss' claim and no issuer was specified",
        )
    config = registry.by_iss(iss)
    if config is None:
        raise reject(
            "unknown_issuer",
            "the token's issuer is not in the gateway allow-list",
        )
    return config


def _check_header_policy(header: dict[str, Any], config: IssuerConfig) -> tuple[str, str]:
    alg = header.get("alg")
    if not isinstance(alg, str) or not alg:
        raise reject("invalid_header", "'alg' header must be a non-empty string")
    if alg.lower() == "none":
        raise reject(
            "algorithm_not_allowed",
            "the 'none' algorithm is never accepted",
            alg=alg,
        )
    if alg not in config.algorithms:
        raise reject(
            "algorithm_not_allowed",
            "token algorithm is not in the issuer's explicit allow-list",
            alg=alg,
            allowed_algorithms=sorted(config.algorithms),
        )
    typ = header.get("typ")
    if typ is not None and typ.upper() not in ("JWT", "APPLICATION/JWT"):
        raise reject("invalid_header", "unsupported 'typ' header", typ=typ)
    for param in _FORBIDDEN_HEADER_PARAMS:
        if param in header:
            raise reject(
                "header_key_reference_forbidden",
                f"header parameter {param!r} is not permitted; keys come only "
                f"from the issuer's configured JWKS URI",
                parameter=param,
            )
    cty = header.get("cty")
    if isinstance(cty, str) and cty.lower() in ("jwt", "application/jwt"):
        # Nested JWTs are not supported by this gateway.
        raise reject("invalid_header", "nested JWT is not supported")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise reject("missing_kid", "token header must carry a non-empty 'kid'")
    crit = header.get("crit")
    if crit is not None:
        if not isinstance(crit, list) or not all(isinstance(c, str) for c in crit):
            raise reject("invalid_header", "'crit' must be a list of strings")
        raise reject(
            "critical_header_unsupported",
            "no critical header extensions are understood by this gateway",
            crit=crit,
        )
    return alg, kid


async def verify_token(
    token: str,
    registry: IssuerRegistry,
    *,
    explicit_issuer_id: Optional[str] = None,
    now: Optional[float] = None,
) -> VerifiedToken:
    """Verify a compact-serialized JWT against local issuer configuration."""
    fingerprint = token_fingerprint(token if isinstance(token, str) else "")
    current = time.time() if now is None else now

    header_raw, payload_raw, signature, signing_input = _split(token)
    header = _json_object(header_raw, "malformed_header", "JWT header is not a JSON object")
    payload = _json_object(payload_raw, "malformed_payload", "JWT payload is not a JSON object")

    config = _select_issuer(header, payload, registry, explicit_issuer_id)
    alg, kid = _check_header_policy(header, config)

    if not signature:
        raise reject("empty_signature", "token has an empty signature segment")

    cache = registry.cache_for(config)
    try:
        key = await cache.get_verification_key(kid, alg)
    except TokenError:
        raise
    except Exception as exc:  # defensive: never leak internal error text raw
        raise reject("jwks_error", f"JWKS key resolution failed: {type(exc).__name__}")

    if not verify_signature(alg, key, signing_input, signature):
        raise reject(
            "invalid_signature",
            "signature did not verify against the issuer JWKS key",
            kid=kid,
            alg=alg,
        )

    # The payload is authenticated from here on.
    claim_checks.check_issuer(payload, config.iss)
    claim_checks.check_audience(payload, config.audience)
    claim_checks.check_time_claims(payload, leeway=config.leeway_seconds, now=current)

    return VerifiedToken(
        issuer_id=config.issuer_id,
        iss=config.iss,
        kid=kid,
        alg=alg,
        claims=payload,
        fingerprint=fingerprint,
    )
