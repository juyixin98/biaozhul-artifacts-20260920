from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import settings
from app.errors import VaultError
from app.routers import audit as audit_router
from app.routers import drafts as drafts_router
from app.routers import sign as sign_router
from app.routers import users as users_router
from app.routers import wallets as wallets_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# The application never logs request bodies. Crypto/SQLAlchemy loggers are
# silenced so raw parameters cannot leak through statement logging.
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

app = FastAPI(
    title="VaultCommand",
    description="Offline EVM transaction signing backend (local testing only).",
    version="1.0.0",
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(VaultError)
async def vault_error_handler(request: Request, exc: VaultError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "request_id": getattr(request.state, "request_id", None),
            }
        },
    )


# Fields whose submitted value must never be echoed back in an error response.
_SENSITIVE_FIELDS = {"private_key_hex", "api_key", "authorization"}


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Sanitized 422 handler: field paths are returned but user input is NOT
    echoed, so a malformed private key can never appear in an error body."""
    issues = []
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", []) if p != "body"]
        path = ".".join(loc)
        if any(part in _SENSITIVE_FIELDS for part in (err.get("loc") or [])):
            issues.append({"field": path, "message": "invalid value"})
        else:
            issues.append({"field": path, "message": err.get("msg", "invalid value")})
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "request validation failed",
                "details": issues,
                "request_id": getattr(request.state, "request_id", None),
            }
        },
    )


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "environment": settings.environment}


app.include_router(users_router.router)
app.include_router(wallets_router.router)
app.include_router(drafts_router.router)
app.include_router(sign_router.router)
app.include_router(audit_router.router)
