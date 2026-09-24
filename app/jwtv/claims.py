"""Claims validation (RFC 7519).

Time checks support an injected clock so tests can exercise exact
boundaries; leeway is per-issuer configuration.
"""

from __future__ import annotations

from typing import Any, Callable

from .errors import reject

Clock = Callable[[], float]


def require_json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise reject("invalid_payload", "JWT payload must be a JSON object")
    return value


def is_int(value: Any) -> bool:
    # bool is a subclass of int; reject it explicitly.
    return isinstance(value, int) and not isinstance(value, bool)


def check_time_claims(
    claims: dict[str, Any],
    *,
    leeway: int,
    now: float,
) -> None:
    exp = claims.get("exp")
    if exp is not None:
        if not is_int(exp):
            raise reject("invalid_claim", "'exp' must be a NumericDate", claim="exp")
        if now > exp + leeway:
            raise reject(
                "token_expired",
                "token is expired",
                exp=exp,
                now=int(now),
                leeway=leeway,
            )
    nbf = claims.get("nbf")
    if nbf is not None:
        if not is_int(nbf):
            raise reject("invalid_claim", "'nbf' must be a NumericDate", claim="nbf")
        if now + leeway < nbf:
            raise reject(
                "token_not_yet_valid",
                "token is not valid yet (nbf in the future)",
                nbf=nbf,
                now=int(now),
                leeway=leeway,
            )
    iat = claims.get("iat")
    if iat is not None:
        if not is_int(iat):
            raise reject("invalid_claim", "'iat' must be a NumericDate", claim="iat")
        if now + leeway < iat:
            raise reject(
                "issued_in_future",
                "token 'iat' is in the future",
                iat=iat,
                now=int(now),
                leeway=leeway,
            )


def audiences(claims: dict[str, Any]) -> list[str]:
    aud = claims.get("aud")
    if isinstance(aud, str):
        return [aud]
    if isinstance(aud, list) and all(isinstance(a, str) for a in aud):
        return list(aud)
    return []


def check_audience(claims: dict[str, Any], expected: str) -> None:
    if expected not in audiences(claims):
        raise reject(
            "invalid_audience",
            "token 'aud' does not include the expected audience",
            expected_audience=expected,
        )


def check_issuer(claims: dict[str, Any], expected_iss: str) -> None:
    iss = claims.get("iss")
    if not isinstance(iss, str):
        raise reject("invalid_claim", "'iss' must be a string", claim="iss")
    if iss != expected_iss:
        raise reject(
            "issuer_mismatch",
            "token 'iss' does not match the configured issuer",
            expected_issuer=expected_iss,
        )
