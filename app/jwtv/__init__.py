"""Core JWT/JWK/JWKS verification primitives built directly on ``cryptography``.

No third-party JWT library is used: the only signature algorithms that can
ever execute are the ones an issuer explicitly allows, and the key type is
pinned to the algorithm family, which removes the classic
HMAC-vs-RS/ES algorithm-confusion class of attacks by construction.
"""

from .errors import TokenError
from .verifier import verify_token, VerifiedToken
from .jwks import IssuerRegistry, IssuerCache, HttpJWKSFetcher

__all__ = [
    "TokenError",
    "verify_token",
    "VerifiedToken",
    "IssuerRegistry",
    "IssuerCache",
    "HttpJWKSFetcher",
]
