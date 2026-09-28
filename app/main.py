"""Application factory and entrypoint.

Run locally with::

    uvicorn app.main:create_app --factory --reload
    # or: python -m app.main
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api import router
from .config import Settings
from .corpus import default_lexicon
from .dependencies import AppState
from .diagnostics import DiagnosticLog
from .mining.lexer import Lexer
from .service import DocumentService
from .storage import Database


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = FastAPI(
        title="Typed Bracket Structure Index",
        version="0.1.0",
        description=(
            "Chunk-summarized bracket index with local-edit invalidation, "
            "match jump, shortest unbalanced interval and full-scan verify."
        ),
    )
    db = Database(settings.db_path)
    lexer = Lexer(default_lexicon())
    state = AppState(
        settings=settings,
        service=DocumentService(
            db, lexicon=default_lexicon(), chunk_size=settings.chunk_size
        ),
        diagnostics=DiagnosticLog(),
        lexer=lexer,
    )
    app.state.app_state = state
    app.include_router(router)

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:
        # Never leak internals/stack payloads to clients.
        logging.getLogger("bracket_index").exception("unhandled error")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "status": "rejected",
                "request_id": None,
                "reason": "INTERNAL_ERROR",
                "data": None,
                "error": {"category": "INTERNAL_ERROR",
                          "message": "internal error", "state": {}},
            },
        )

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "chunk_size": settings.chunk_size}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
