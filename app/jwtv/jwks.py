"""Per-issuer JWKS fetching and caching.

Design notes:

* Each issuer gets its own :class:`IssuerCache` — key state never leaks
  between issuers, so a ``kid`` that happens to be reused by two issuers
  cannot collide.
* The fetch URL comes **only** from local issuer configuration. Tokens are
  never allowed to influence where keys are fetched from (``jku``/``x5u``
  headers are rejected upstream).
* A key id that appears more than once in a JWK Set is ambiguous and the
  whole set is rejected ("duplicate_kid").
* Rotation: keys have an independent TTL.  An unknown ``kid`` triggers at
  most one refresh per cooldown interval ("negative caching") so an
  attacker cannot turn token traffic into a JWKS-fetch storm.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import httpx

from . import jwk
from .errors import TokenError, reject

Fetcher = Callable[[str], Awaitable[str]]


class JWKSFetchError(Exception):
    """Raised when a JWK Set document cannot be fetched or parsed."""


class DuplicateKidError(Exception):
    """Raised when a JWK Set contains the same kid more than once."""


@dataclass(frozen=True)
class IssuerConfig:
    issuer_id: str
    iss: str
    jwks_uri: str
    audience: str
    algorithms: frozenset[str]
    cache_ttl_seconds: int = 600
    negative_cooldown_seconds: int = 30
    leeway_seconds: int = 0
    allow_http: bool = False

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "IssuerConfig":
        try:
            cfg = IssuerConfig(
                issuer_id=str(raw["issuer_id"]),
                iss=str(raw["iss"]),
                jwks_uri=str(raw["jwks_uri"]),
                audience=str(raw["audience"]),
                algorithms=frozenset(raw["algorithms"]),
                cache_ttl_seconds=int(raw.get("cache_ttl_seconds", 600)),
                negative_cooldown_seconds=int(raw.get("negative_cooldown_seconds", 30)),
                leeway_seconds=int(raw.get("leeway_seconds", 0)),
                allow_http=bool(raw.get("allow_http", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid issuer entry: {exc}") from exc
        if not cfg.algorithms:
            raise ValueError("issuer must allow at least one algorithm")
        unsupported = cfg.algorithms - jwk.SUPPORTED_ALGORITHMS
        if unsupported:
            raise ValueError(
                f"issuer {cfg.issuer_id}: unsupported algorithms "
                f"{sorted(unsupported)}; supported: {sorted(jwk.SUPPORTED_ALGORITHMS)}"
            )
        if not cfg.jwks_uri.startswith(("https://", "http://")):
            raise ValueError(f"issuer {cfg.issuer_id}: jwks_uri must be http(s)")
        if cfg.jwks_uri.startswith("http://") and not cfg.allow_http:
            raise ValueError(
                f"issuer {cfg.issuer_id}: plain http jwks_uri requires "
                f"explicit \"allow_http\": true (local development only)"
            )
        for name, value in (
            ("cache_ttl_seconds", cfg.cache_ttl_seconds),
            ("negative_cooldown_seconds", cfg.negative_cooldown_seconds),
            ("leeway_seconds", cfg.leeway_seconds),
        ):
            if value < 0:
                raise ValueError(f"issuer {cfg.issuer_id}: {name} must be >= 0")
        return cfg


@dataclass
class _KeyEntry:
    public_key: Any
    alg: str  # the alg declared in the JWK, validated against the allow-list
    kty: str


@dataclass
class CacheSnapshot:
    issuer_id: str
    keys: dict[str, dict[str, str]]
    fetched_at: Optional[float]
    expires_at: Optional[float]
    last_error: Optional[str]
    negative_refresh_at: Optional[float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "issuer_id": self.issuer_id,
            "keys": self.keys,
            "fetched_at": self.fetched_at,
            "expires_at": self.expires_at,
            "last_error": self.last_error,
            "negative_refresh_at": self.negative_refresh_at,
        }


class IssuerCache:
    """JWKS state for one issuer."""

    def __init__(
        self,
        config: IssuerConfig,
        fetcher: Fetcher,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._fetcher = fetcher
        self._clock = clock
        self._keys: dict[str, _KeyEntry] = {}
        self._fetched_at: Optional[float] = None
        self._expires_at: Optional[float] = None
        self._last_error: Optional[str] = None
        self._negative_refresh_at: Optional[float] = None
        self._refreshing: asyncio.Lock = asyncio.Lock()

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def snapshot(self) -> CacheSnapshot:
        return CacheSnapshot(
            issuer_id=self.config.issuer_id,
            keys={
                kid: {"kty": entry.kty, "alg": entry.alg}
                for kid, entry in self._keys.items()
            },
            fetched_at=self._fetched_at,
            expires_at=self._expires_at,
            last_error=self._last_error,
            negative_refresh_at=self._negative_refresh_at,
        )

    async def get_verification_key(self, kid: str, alg: str) -> Any:
        """Resolve ``kid`` to a usable public key for ``alg``.

        Refreshes the JWKS on cache miss or TTL expiry (rate limited). A
        refresh failure while a previously-good key set is cached falls
        back to the stale key rather than rejecting valid traffic; a
        successful refresh that still lacks ``kid`` arms the negative
        cooldown so unknown kids cannot trigger a fetch storm.
        """
        entry = self._keys.get(kid)
        now = self._clock()
        if (entry is None or self._is_stale(now)) and self._may_refresh(now):
            try:
                await self.refresh()
            except TokenError:
                # Only tolerate a failed refresh when a previously-good key
                # exists; a cold-cache failure cannot serve anyone.
                if entry is None:
                    raise
            else:
                # A successful fetch is authoritative: the local entry is
                # replaced even when the new set lacks the kid.
                entry = self._keys.get(kid)
                if entry is None:
                    self._negative_refresh_at = (
                        now + self.config.negative_cooldown_seconds
                    )

        if entry is None:
            raise reject(
                "unknown_kid",
                "no key with the token's kid is published by the issuer",
                kid=kid,
            )
        if entry.alg != alg:
            raise reject(
                "key_alg_mismatch",
                "JWK 'alg' does not match the token header algorithm",
                kid=kid,
                token_alg=alg,
                jwk_alg=entry.alg,
            )
        if jwk.alg_family(alg) != jwk.alg_family(entry.alg):
            raise reject(
                "key_type_mismatch",
                "key type does not match the signature algorithm",
                kid=kid,
            )
        return entry.public_key

    def _is_stale(self, now: float) -> bool:
        return self._expires_at is not None and now >= self._expires_at

    def _may_refresh(self, now: float) -> bool:
        return self._negative_refresh_at is None or now >= self._negative_refresh_at

    async def refresh(self) -> None:
        """Fetch and replace the key set. Serialized per issuer."""
        async with self._refreshing:
            now = self._clock()
            try:
                document = await self._fetcher(self.config.jwks_uri)
                parsed = parse_jwks(document, self.config.algorithms)
            except DuplicateKidError as exc:
                self._last_error = str(exc)
                self._negative_refresh_at = now + self.config.negative_cooldown_seconds
                raise reject(
                    "duplicate_kid",
                    "issuer JWKS assigns the same kid to more than one key",
                    reason=str(exc),
                )
            except JWKSFetchError as exc:
                self._last_error = str(exc)
                self._negative_refresh_at = now + self.config.negative_cooldown_seconds
                # A failed refresh never wipes previously-good keys: an
                # issuer endpoint outage must not invalidate valid tokens.
                if self._keys:
                    self._expires_at = max(
                        self._negative_refresh_at or 0,
                        self._expires_at or 0,
                    )
                raise reject(
                    "jwks_unavailable",
                    "issuer JWKS could not be loaded",
                    reason=str(exc),
                )
            self._keys = parsed
            self._fetched_at = now
            self._expires_at = now + self.config.cache_ttl_seconds
            self._last_error = None
            self._negative_refresh_at = None

    async def force_refresh(self) -> None:
        """Admin override: bypass cooldown/TTL."""
        self._negative_refresh_at = None
        self._expires_at = None
        await self.refresh()


def parse_jwks(
    document: str,
    allowed_algorithms: frozenset[str],
) -> dict[str, _KeyEntry]:
    """Parse a JWK Set JSON document into ``{kid: _KeyEntry}``.

    * Keys that are not signature keys, or whose declared ``alg`` is not
      allowed for this issuer, are skipped.
    * A repeated ``kid`` among the *usable* keys is ambiguous and fatal.
    """
    try:
        body = _json_loads_strict(document)
    except ValueError as exc:
        raise JWKSFetchError(f"JWKS is not valid JSON: {exc}") from exc
    if not isinstance(body, dict) or not isinstance(body.get("keys"), list):
        raise JWKSFetchError("JWKS must be a JSON object with a 'keys' array")

    keys: dict[str, _KeyEntry] = {}
    seen_kids: set[str] = set()
    for raw in body["keys"]:
        if not isinstance(raw, dict):
            continue
        if raw.get("use") not in (None, "sig"):
            continue
        ops = raw.get("key_ops")
        if ops is not None:
            if not isinstance(ops, list) or not all(isinstance(o, str) for o in ops):
                raise JWKSFetchError("invalid 'key_ops' in JWK")
            if not any(o in ("verify",) for o in ops):
                continue
        alg = raw.get("alg")
        kid = raw.get("kid")
        if not isinstance(alg, str) or not isinstance(kid, str):
            continue
        if alg not in allowed_algorithms:
            # A key for an algorithm the gateway does not allow for this
            # issuer is simply invisible — it can never be selected.
            continue
        if kid in seen_kids:
            raise DuplicateKidError(
                f"JWKS contains kid {kid!r} more than once among usable keys"
            )
        seen_kids.add(kid)
        try:
            public_key = jwk.jwk_to_public_key(raw)
        except ValueError as exc:
            raise JWKSFetchError(f"invalid JWK for kid {kid!r}: {exc}") from exc
        if jwk.alg_family(alg) != _family_of_key(public_key):
            raise JWKSFetchError(
                f"JWK kid {kid!r}: alg {alg} does not match its key type"
            )
        keys[kid] = _KeyEntry(public_key=public_key, alg=alg, kty=raw["kty"])
    return keys


def _family_of_key(key: Any) -> str:
    from cryptography.hazmat.primitives.asymmetric import ec, rsa

    if isinstance(key, rsa.RSAPublicKey):
        return "RSA"
    if isinstance(key, ec.EllipticCurvePublicKey):
        return "EC"
    return "UNKNOWN"


def _json_loads_strict(text: str) -> Any:
    import json
    import math

    def _reject_constant(constant: str) -> None:
        raise ValueError(f"invalid constant {constant}")

    value = json.loads(text, parse_constant=_reject_constant)

    def check(obj: Any) -> None:
        if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            raise ValueError("non-finite number")
        if isinstance(obj, dict):
            for k, v in obj.items():
                check(k)
                check(v)
        elif isinstance(obj, list):
            for v in obj:
                check(v)

    check(value)
    return value


class IssuerRegistry:
    """Holds all configured issuers and one isolated cache per issuer."""

    def __init__(
        self,
        configs: list[IssuerConfig],
        fetcher: Fetcher,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._by_id: dict[str, IssuerConfig] = {}
        self._by_iss: dict[str, IssuerConfig] = {}
        self._caches: dict[str, IssuerCache] = {}
        for config in configs:
            if config.issuer_id in self._by_id:
                raise ValueError(f"duplicate issuer_id {config.issuer_id!r}")
            if config.iss in self._by_iss:
                raise ValueError(f"duplicate iss {config.iss!r}")
            self._by_id[config.issuer_id] = config
            self._by_iss[config.iss] = config
            self._caches[config.issuer_id] = IssuerCache(config, fetcher, clock=clock)

    def get(self, issuer_id: str) -> Optional[IssuerConfig]:
        return self._by_id.get(issuer_id)

    def by_iss(self, iss: str) -> Optional[IssuerConfig]:
        return self._by_iss.get(iss)

    def all_configs(self) -> list[IssuerConfig]:
        return list(self._by_id.values())

    def cache_for(self, config: IssuerConfig) -> IssuerCache:
        return self._caches[config.issuer_id]

    def cache_by_id(self, issuer_id: str) -> Optional[IssuerCache]:
        config = self._by_id.get(issuer_id)
        return self._caches[config.issuer_id] if config else None


class HttpJWKSFetcher:
    """Fetches JWK Set documents over HTTP(S).

    Security defaults: no redirects (a redirect could move key retrieval
    onto an attacker host), tight size limit and timeout, GET only.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 5.0,
        max_bytes: int = 65_536,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._max_bytes = max_bytes
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds, follow_redirects=False
        )

    async def __call__(self, url: str) -> str:
        try:
            response = await self._client.get(url, headers={"accept": "application/json"})
        except httpx.HTTPError as exc:
            raise JWKSFetchError(f"HTTP request to JWKS endpoint failed: {type(exc).__name__}")
        if response.status_code != 200:
            raise JWKSFetchError(f"JWKS endpoint returned HTTP {response.status_code}")
        content_type = response.headers.get("content-type", "")
        if content_type and "json" not in content_type.lower():
            raise JWKSFetchError(f"unexpected JWKS content-type {content_type!r}")
        body = response.text
        if len(body.encode("utf-8")) > self._max_bytes:
            raise JWKSFetchError("JWKS document exceeds size limit")
        return body

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

