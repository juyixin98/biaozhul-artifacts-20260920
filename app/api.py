"""FastAPI HTTP layer.

Failure policy: domain errors are mapped to their declared status code with a
stable error code; request-body schema errors get the same structured shape;
unexpected errors are logged with the run id and returned as 500 — never as a
silent success.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from app import __version__
from app.errors import SPDError
from app.observability import get_run_logger
from app.service import build_service
from app.models import CorpusCreateRequest, MineRequest


def create_app(*, db_path: str | None = None) -> FastAPI:
    app = FastAPI(
        title="Sequential Pattern Mining API",
        version=__version__,
        description="Frequent sequential pattern mining with separate "
                    "position/time max-gap and sequence-identity support.",
    )
    service = build_service(db_path=db_path)
    app.state.service = service

    # ------------------------------------------------------------- error maps

    @app.exception_handler(SPDError)
    async def _spm_error_handler(_: Request, exc: SPDError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_request_body",
                "message": "request payload failed schema validation",
                "details": {"issues": exc.errors()},
            },
        )

    @app.exception_handler(Exception)
    async def _unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        run_id = getattr(request.state, "run_id", "unbound")
        logger = get_run_logger(run_id, file_logging=True)
        logger.error(
            "unhandled-exception",
            path=request.url.path,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        logger.close()
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "message": "unexpected internal failure; see run log",
                "details": {"run_id": logger.run_id},
            },
        )

    # ----------------------------------------------------------------- routes

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": "spm-backend", "version": __version__}

    @app.get("/v1/fixtures")
    async def list_fixtures() -> dict:
        from app.corpus.fixtures import FIXTURES
        return {"fixtures": sorted(FIXTURES)}

    # Routes below perform blocking SQLite / CPU mining work, so they are plain
    # ``def``: FastAPI runs them in its threadpool instead of blocking the
    # event loop (the project uses sync sqlite3, not an async driver).

    @app.post("/v1/fixtures/{fixture_name}", status_code=201)
    def create_fixture(fixture_name: str) -> dict:
        corpus = service.load_fixture(fixture_name)
        return {"corpus_id": corpus.corpus_id, "name": corpus.name,
                "sequences": corpus.size}

    @app.post("/v1/corpora", status_code=201)
    def create_corpus(body: CorpusCreateRequest) -> dict:
        corpus = service.create_corpus(body)
        return {
            "corpus_id": corpus.corpus_id,
            "name": corpus.name,
            "description": corpus.description,
            "sequence_count": corpus.size,
        }

    @app.get("/v1/corpora")
    def list_corpora() -> dict:
        return {"corpora": service.list_corpora()}

    @app.get("/v1/corpora/{corpus_id}")
    def get_corpus(corpus_id: str) -> dict:
        corpus = service.get_corpus(corpus_id)
        return {
            "corpus_id": corpus.corpus_id,
            "name": corpus.name,
            "description": corpus.description,
            "sequences": [
                {
                    "sequence_id": s.sequence_id,
                    "events": [e.model_dump() for e in s.events],
                }
                for s in corpus.sequences
            ],
        }

    @app.delete("/v1/corpora/{corpus_id}", status_code=204)
    def delete_corpus(corpus_id: str) -> Response:
        service.delete_corpus(corpus_id)
        return Response(status_code=204)

    @app.post("/v1/mine")
    def mine(request: Request, body: MineRequest) -> dict:
        logger = get_run_logger(prefix="mine")
        request.state.run_id = logger.run_id  # bind so a 500 handler keeps the id
        logger.info("request-received", path="/v1/mine", body=body.model_dump())
        try:
            response = request.app.state.service.mine(body, logger)
        except Exception:
            # Re-raise after closing the logger; the global handler logs it.
            logger.close()
            raise
        logger.close()
        return response.model_dump(mode="json")

    return app


app = create_app()
