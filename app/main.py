"""FastAPI application: HTTP surface for the verification gateway.

Endpoints
----------
GET  /healthz                       liveness probe
GET  /v1/issuers                    configured issuers and their allow-lists
POST /v1/verify                     verify a token (header or JSON body)
GET  /admin/issuers/{id}/cache      inspect one issuer's JWKS cache
POST /admin/issuers/{id}/refresh    force a JWKS refresh

Run with::

    uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8080
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import load_issuer_configs, resolve_config_path
from .jwtv import TokenError
from .jwtv.jwks import HttpJWKSFetcher, IssuerRegistry
from .jwtv.verifier import token_fingerprint, verify_token
from .logging import audit_fields, configure_logging

BEARER_PREFIX = "Bearer "


class VerifyRequest(BaseModel):
    token: str = Field(
        ...,
        min_length=1,
        description="Compact-serialized JWT. Alternatively send "
        "'Authorization: Bearer <token>'.",
    )
    issuer_id: Optional[str] = Field(
        default=None,
        description="Pin verification to a configured issuer_id; by default "
        "the issuer is selected from the token 'iss' claim.",
    )


def create_app(
    configs: Optional[list] = None,
    *,
    fetcher: Optional[Any] = None,
    config_path: Optional[str] = None,
    log_level: str = "INFO",
) -> FastAPI:
    logger = configure_logging(log_level)
    if configs is None:
        configs = load_issuer_configs(config_path or resolve_config_path())

    owns_fetcher = fetcher is None
    http_fetcher = fetcher or HttpJWKSFetcher()
    registry = IssuerRegistry(configs, http_fetcher)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.registry = registry
        try:
            yield
        finally:
            if owns_fetcher and isinstance(http_fetcher, HttpJWKSFetcher):
                await http_fetcher.aclose()

    app = FastAPI(
        title="JWT Multi-Issuer Verification Gateway",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.registry = registry

    @app.exception_handler(TokenError)
    async def token_error_handler(request: Request, exc: TokenError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_body(),
            headers={"WWW-Authenticate": f'Bearer error="{exc.code}"'},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "error": {
                    "code": "invalid_request",
                    "message": "request body failed validation",
                    "details": {"errors": exc.errors()},
                },
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        # Never leak internal exception text or stack traces to callers;
        # the error log stays server-side and carries no token material.
        logger.exception("unhandled_error", extra={"fields": {"path": request.url.path}})
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "error": {"code": "internal_error", "message": "internal error"},
            },
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/issuers")
    async def list_issuers() -> dict[str, Any]:
        return {
            "issuers": [
                {
                    "issuer_id": c.issuer_id,
                    "iss": c.iss,
                    "audience": c.audience,
                    "allowed_algorithms": sorted(c.algorithms),
                }
                for c in registry.all_configs()
            ]
        }

    @app.post("/v1/verify")
    async def verify(
        body: Optional[VerifyRequest] = None,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, Any]:
        token: Optional[str] = None
        issuer_id: Optional[str] = None
        if body is not None and body.token:
            token = body.token
            issuer_id = body.issuer_id
        elif authorization:
            if not authorization.startswith(BEARER_PREFIX):
                raise TokenError(
                    code="invalid_request",
                    message="Authorization header must use Bearer scheme",
                    status_code=400,
                )
            token = authorization[len(BEARER_PREFIX) :].strip()
        if not token:
            raise TokenError(
                code="missing_token",
                message='provide a JSON {"token": ...} body or an '
                "'Authorization: Bearer <token>' header",
                status_code=400,
            )

        fingerprint = token_fingerprint(token)
        try:
            result = await verify_token(token, registry, explicit_issuer_id=issuer_id)
        except TokenError as exc:
            logger.warning(
                "token rejected",
                extra={
                    "fields": audit_fields(
                        outcome="rejected",
                        issuer_id=issuer_id,
                        kid=None,
                        alg=None,
                        fingerprint=fingerprint,
                        error_code=exc.code,
                    )
                },
            )
            raise exc
        logger.info(
            "token accepted",
            extra={
                "fields": audit_fields(
                    outcome="accepted",
                    issuer_id=result.issuer_id,
                    kid=result.kid,
                    alg=result.alg,
                    fingerprint=result.fingerprint,
                )
            },
        )
        return {
            "status": "ok",
            "issuer_id": result.issuer_id,
            "iss": result.iss,
            "kid": result.kid,
            "alg": result.alg,
            "claims": result.claims,
        }

    @app.get("/admin/issuers/{issuer_id}/cache")
    async def issuer_cache(issuer_id: str) -> dict[str, Any]:
        cache = registry.cache_by_id(issuer_id)
        if cache is None:
            raise TokenError(
                code="unknown_issuer",
                message="issuer is not configured on this gateway",
                status_code=404,
            )
        return cache.snapshot().to_dict()

    @app.post("/admin/issuers/{issuer_id}/refresh")
    async def issuer_refresh(issuer_id: str) -> dict[str, Any]:
        cache = registry.cache_by_id(issuer_id)
        if cache is None:
            raise TokenError(
                code="unknown_issuer",
                message="issuer is not configured on this gateway",
                status_code=404,
            )
        await cache.force_refresh()
        return cache.snapshot().to_dict()

    return app


def main() -> None:  # pragma: no cover - process entry point
    import uvicorn

    uvicorn.run(
        create_app(), host="127.0.0.1", port=8080, log_config=None
    )


if __name__ == "__main__":  # pragma: no cover
    main()
