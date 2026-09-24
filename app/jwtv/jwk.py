"""JWK (RFC 7517) parsing into ``cryptography`` public keys.

Only RSA and EC keys are supported, and only for the asymmetric signature
algorithms this gateway allows.  Symmetric keys (``kty``, ``oct``) are
rejected outright so an HMAC secret can never be smuggled in as a
verification key.
"""

from __future__ import annotations

from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec, rsa

from .b64 import b64url_decode_uint, b64url_encode_uint

#: The complete set of signature algorithms this gateway can ever execute.
SUPPORTED_ALGORITHMS: frozenset[str] = frozenset(
    {"RS256", "RS384", "RS512", "ES256", "ES384"}
)

ALG_FAMILY: dict[str, str] = {
    "RS256": "RSA",
    "RS384": "RSA",
    "RS512": "RSA",
    "ES256": "EC",
    "ES384": "EC",
}

#: EC curve required by each ES* algorithm (JWA, RFC 7518 section 3.4).
ALG_CURVE: dict[str, ec.EllipticCurve] = {
    "ES256": ec.SECP256R1(),
    "ES384": ec.SECP384R1(),
}

MIN_RSA_MODULUS_BITS = 2048


def alg_family(alg: str) -> str:
    return ALG_FAMILY[alg]


def jwk_to_public_key(jwk: dict[str, Any]) -> rsa.RSAPublicKey | ec.EllipticCurvePublicKey:
    """Convert a public JWK dict into a ``cryptography`` public key.

    Raises ``ValueError`` for anything that is not a well-formed public RSA
    or EC key.  Private material (``d``) is ignored if present.
    """
    if not isinstance(jwk, dict):
        raise ValueError("JWK must be a JSON object")
    kty = jwk.get("kty")
    if kty == "RSA":
        return _rsa_public_key(jwk)
    if kty == "EC":
        return _ec_public_key(jwk)
    raise ValueError(f"unsupported key type {kty!r}")


def _rsa_public_key(jwk: dict[str, Any]) -> rsa.RSAPublicKey:
    try:
        n = b64url_decode_uint(jwk["n"])
        e = b64url_decode_uint(jwk["e"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("RSA JWK requires base64url 'n' and 'e'") from exc
    if n.bit_length() < MIN_RSA_MODULUS_BITS:
        raise ValueError(f"RSA modulus below {MIN_RSA_MODULUS_BITS} bits")
    if e < 3 or e % 2 == 0:
        raise ValueError("invalid RSA public exponent")
    try:
        return rsa.RSAPublicNumbers(e=e, n=n).public_key()
    except ValueError as exc:
        raise ValueError(f"invalid RSA public key: {exc}") from exc


def _ec_public_key(jwk: dict[str, Any]) -> ec.EllipticCurvePublicKey:
    crv_name = jwk.get("crv")
    curves = {"P-256": ec.SECP256R1, "P-384": ec.SECP384R1}
    curve_cls = curves.get(crv_name)
    if curve_cls is None:
        raise ValueError(f"unsupported EC curve {crv_name!r}")
    try:
        x = b64url_decode_uint(jwk["x"])
        y = b64url_decode_uint(jwk["y"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("EC JWK requires base64url 'x' and 'y'") from exc
    try:
        return ec.EllipticCurvePublicNumbers(x=x, y=y, curve=curve_cls()).public_key()
    except ValueError as exc:
        raise ValueError(f"invalid EC public key: {exc}") from exc


def public_jwk_from_private_key(private_key: Any, kid: str, alg: str) -> dict[str, Any]:
    """Build a public JWK from a ``cryptography`` private key (test/demo use)."""
    public = private_key.public_key()
    if isinstance(public, rsa.RSAPublicKey):
        numbers = public.public_numbers()
        return {
            "kty": "RSA",
            "use": "sig",
            "kid": kid,
            "alg": alg,
            "n": b64url_encode_uint(numbers.n),
            "e": b64url_encode_uint(numbers.e),
        }
    if isinstance(public, ec.EllipticCurvePublicKey):
        numbers = public.public_numbers()
        # curve name lookup by class
        curve_names = {"secp256r1": "P-256", "secp384r1": "P-384"}
        name = curve_names.get(numbers.curve.name)
        if name is None:
            raise ValueError(f"unsupported curve {numbers.curve.name}")
        return {
            "kty": "EC",
            "use": "sig",
            "kid": kid,
            "alg": alg,
            "crv": name,
            "x": b64url_encode_uint(numbers.x),
            "y": b64url_encode_uint(numbers.y),
        }
    raise ValueError(f"unsupported key type {type(private_key)!r}")
