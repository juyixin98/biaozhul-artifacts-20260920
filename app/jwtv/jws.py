"""JWS signing/verification primitives on top of ``cryptography``.

The verifier only ever dispatches on the *allow-listed* algorithm string
taken from the issuer configuration after it has been matched against the
token header — there is no code path in which a token can select an
algorithm (or key type) the issuer did not configure.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils

_HASHES: dict[str, Any] = {
    "RS256": hashes.SHA256,
    "RS384": hashes.SHA384,
    "RS512": hashes.SHA512,
    "ES256": hashes.SHA256,
    "ES384": hashes.SHA384,
}

_EC_SIG_LEN: dict[str, int] = {"ES256": 64, "ES384": 96}


def verify_signature(alg: str, key: Any, signing_input: bytes, signature: bytes) -> bool:
    """Return True iff ``signature`` is valid for ``signing_input``.

    ``alg`` must already be allow-listed for the issuer; ``key`` must be of
    the family that matches ``alg`` (enforced again here defensively).
    """
    try:
        if alg.startswith("RS"):
            if not isinstance(key, rsa.RSAPublicKey):
                return False
            key.verify(signature, signing_input, padding.PKCS1v15(), _HASHES[alg]())
            return True
        if alg.startswith("ES"):
            if not isinstance(key, ec.EllipticCurvePublicKey):
                return False
            expected_len = _EC_SIG_LEN[alg]
            if len(signature) != expected_len:
                return False
            r = int.from_bytes(signature[: expected_len // 2], "big")
            s = int.from_bytes(signature[expected_len // 2 :], "big")
            der = utils.encode_dss_signature(r, s)
            key.verify(der, signing_input, ec.ECDSA(_HASHES[alg]()))
            return True
    except (InvalidSignature, ValueError):
        return False
    return False


def sign(alg: str, key: Any, signing_input: bytes) -> bytes:
    """Produce a JWS signature (used by tests and the demo IdP)."""
    if alg.startswith("RS"):
        return key.sign(signing_input, padding.PKCS1v15(), _HASHES[alg]())
    if alg.startswith("ES"):
        der = key.sign(signing_input, ec.ECDSA(_HASHES[alg]()))
        r, s = utils.decode_dss_signature(der)
        size = _EC_SIG_LEN[alg] // 2
        return r.to_bytes(size, "big") + s.to_bytes(size, "big")
    if alg.startswith("HS"):
        digestmod = {"HS256": hashlib.sha256, "HS384": hashlib.sha384, "HS512": hashlib.sha512}[alg]
        secret = key if isinstance(key, bytes) else str(key).encode("utf-8")
        return hmac.new(secret, signing_input, digestmod).digest()
    raise ValueError(f"cannot sign with unsupported algorithm {alg!r}")
