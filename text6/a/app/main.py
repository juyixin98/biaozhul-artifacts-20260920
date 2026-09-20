"""ConsentVault FastAPI application entry point.

NOTE: ConsentVault records *traceable authorisation state* and provides an
auditable, event-sourced history.  It does not by itself claim compliance with
or certification under any particular regulation.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sqlalchemy import text

from .config import get_settings
from .db import engine
from .errors import ConsentVaultError
from .management import router as management_router
from .routers import router as api_router

logger = logging.getLogger("consentvault")

# Each entry is executed separately (psycopg2/SQLAlchemy do not batch
# multi-statement strings reliably through the generic driver).
IMMUTABILITY_STATEMENTS = (
    """
    CREATE OR REPLACE FUNCTION consentvault_events_immutable() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'consent_events is an append-only, immutable history; % is not permitted', TG_OP
            USING ERRCODE = 'insufficient_privilege';
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    DROP TRIGGER IF EXISTS trg_consent_events_immutable ON consent_events
    """,
    """
    CREATE TRIGGER trg_consent_events_immutable
        BEFORE UPDATE OR DELETE OR TRUNCATE ON consent_events
        FOR EACH STATEMENT EXECUTE FUNCTION consentvault_events_immutable()
    """,
    """
    CREATE OR REPLACE FUNCTION consentvault_policy_versions_immutable() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'policy_versions are immutable once published; % is not permitted', TG_OP
            USING ERRCODE = 'insufficient_privilege';
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    DROP TRIGGER IF EXISTS trg_policy_versions_immutable ON policy_versions
    """,
    """
    CREATE TRIGGER trg_policy_versions_immutable
        BEFORE UPDATE OR DELETE ON policy_versions
        FOR EACH STATEMENT EXECUTE FUNCTION consentvault_policy_versions_immutable()
    """,
)


def ensure_database_guards() -> None:
    """Idempotently install the append-only triggers (also created by migration)."""
    with engine.begin() as conn:
        for stmt in IMMUTABILITY_STATEMENTS:
            conn.execute(text(stmt))


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        ensure_database_guards()
    except Exception:  # pragma: no cover - startup diagnostics
        logger.exception("could not install database guards; run migrations first")
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="ConsentVault",
        version="1.0.0",
        description=(
            "Auditable consent-record backend. Records traceable authorisation "
            "state (grant/withdrawal/expiry) over an immutable event history. "
            "Does not assert compliance with any specific regulation."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings

    @app.exception_handler(ConsentVaultError)
    async def _handle_vault_error(request: Request, exc: ConsentVaultError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.code, "message": exc.message},
        )

    @app.get("/healthz", tags=["meta"])
    def healthz():
        return {"status": "ok", "service": "consentvault"}

    app.include_router(management_router)
    app.include_router(api_router)
    return app


app = create_app()
