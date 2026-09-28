"""FastAPI application factory and HTTP routes.

Endpoints
---------
GET  /health
GET  /versions
GET  /versions/{version_id}/schema
POST /admin/load-sql          load an inline SQLite script as a new version
POST /admin/load-directory    load fixture files from disk as a new version
POST /query                   answer + symbolic provenance for one query
POST /verify                  inject numeric weights and verify expressions

Every response (including errors) carries the correlation id generated or
honored by the request-id middleware.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ..config import Settings
from ..observability import (
    bind_request_id,
    configure_logging,
    get_logger,
    log_decision,
    new_request_id,
    request_id_var,
)
from ..provenance.loader import FixtureLoadError, load_sql_directory, load_sql_text
from ..provenance.service import QueryError, QueryService
from ..provenance.store import EvidenceStore, VersionNotFound
from .models import LoadDirectoryRequest, LoadSqlRequest, QueryRequest, VerifyRequest


def create_app(settings: Settings) -> FastAPI:
    configure_logging(settings.log_level)
    logger = get_logger("provenance.api")

    app = FastAPI(
        title="Symbolic Provenance for Positive Relational Queries",
        version="1.0.0",
    )
    app.state.settings = settings
    app.state.store = EvidenceStore(settings.db_path)
    app.state.service = QueryService(
        app.state.store, max_query_nodes=settings.max_query_nodes
    )

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        incoming = request.headers.get("x-request-id")  # client-provided
        request_id = bind_request_id(incoming or new_request_id())
        try:
            response = await call_next(request)
        finally:
            # contextvar is task-local; nothing to reset explicitly
            pass
        response.headers["x-request-id"] = request_id
        return response

    def _error(category: str, detail: str, status: int, request_id: str | None):
        log_decision(
            logger,
            "reject" if status < 500 else "undecided",
            request_id=request_id,
            category=category,
            detail=detail,
        )
        return JSONResponse(
            status_code=status,
            content={
                "error": category,
                "category": category,
                "detail": detail,
                "request_id": request_id,
            },
        )

    @app.exception_handler(QueryError)
    async def query_error_handler(request: Request, exc: QueryError):
        status = 404 if exc.category in {"UNKNOWN_VERSION", "NO_INPUT_VERSION"} else 400
        return _error(exc.category, exc.message, status,
                      request_id_var.get())

    @app.exception_handler(FixtureLoadError)
    async def fixture_error_handler(request: Request, exc: FixtureLoadError):
        return _error(exc.category, exc.message, 400,
                      request_id_var.get())

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request, exc: RequestValidationError
    ):
        return _error(
            "MALFORMED_REQUEST",
            jsonable_encoder(exc.errors()), 422, request_id_var.get()
        )

    @app.exception_handler(ValidationError)
    async def validation_error_handler(request: Request, exc: ValidationError):
        return _error(
            "MALFORMED_REQUEST",
            jsonable_encoder(exc.errors()), 422, request_id_var.get()
        )

    @app.exception_handler(sqlite3.Error)
    async def sqlite_error_handler(request: Request, exc: sqlite3.Error):
        # Do not leak SQL internals; the request id ties this to server logs.
        return _error(
            "STORAGE_ERROR", "a storage error occurred", 500,
            request_id_var.get(),
        )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/versions")
    def list_versions(request: Request):
        store: EvidenceStore = request.app.state.store
        with store._connect() as conn:  # noqa: SLF001 - small read-only accessor
            rows = conn.execute(
                "SELECT version_id, label, created_at FROM versions "
                "ORDER BY version_id"
            ).fetchall()
        return {
            "versions": [
                {"version_id": vid, "label": label, "created_at": created_at}
                for vid, label, created_at in rows
            ]
        }

    @app.get("/versions/{version_id}/schema")
    def get_schema(version_id: int, request: Request):
        store: EvidenceStore = request.app.state.store
        try:
            schema = store.fetch_schema(version_id)
        except VersionNotFound:
            raise QueryError(
                "UNKNOWN_VERSION", f"input version {version_id} does not exist"
            )
        return {"version_id": version_id,
                "schema": {name: list(cols) for name, cols in schema.items()}}

    @app.post("/admin/load-sql")
    def load_sql_endpoint(payload: LoadSqlRequest, request: Request):
        store: EvidenceStore = request.app.state.store
        version_id = load_sql_text(payload.sql, store, label=payload.label)
        log_decision(logger, "accept",
                     request_id=request_id_var.get(),
                     version_id=version_id, action="load-sql")
        return {"version_id": version_id, "loaded": store.list_relations(version_id)}

    @app.post("/admin/load-directory")
    def load_directory_endpoint(payload: LoadDirectoryRequest, request: Request):
        settings_obj: Settings = request.app.state.settings
        store: EvidenceStore = request.app.state.store
        directory = payload.directory or settings_obj.fixture_dir
        glob_pattern = payload.glob or settings_obj.fixture_glob
        if not payload.reload:
            latest = store.latest_version()
            if latest is not None:
                return {"version_id": latest, "reused": True,
                        "loaded": store.list_relations(latest)}
        version_id = load_sql_directory(directory, glob_pattern, store, payload.label)
        return {"version_id": version_id, "reused": False,
                "loaded": store.list_relations(version_id)}

    @app.post("/query")
    def query_endpoint(payload: QueryRequest, request: Request):
        service: QueryService = request.app.state.service
        result = service.run(payload.query, payload.version_id)
        log_decision(
            logger, "accept",
            request_id=request_id_var.get(),
            version_id=result["version_id"], answer_count=len(result["rows"]),
        )
        return result

    @app.post("/verify")
    def verify_endpoint(payload: VerifyRequest, request: Request):
        service: QueryService = request.app.state.service
        rows = [row.model_dump() for row in payload.rows]
        verified = service.verify(payload.version_id, rows)
        return {"version_id": service.resolve_version(payload.version_id),
                "verified": verified}

    return app


# Default app used by uvicorn (``uvicorn app.main:app``).
app = create_app(Settings.from_env())
