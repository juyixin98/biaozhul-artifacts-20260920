"""FastAPI entry point wiring the four boundaries together.

Endpoints:

- ``POST /api/rulesets``           — validate, compile, diagnose and store a
  ruleset (201). Errors: 400 INPUT_ERROR, 409 STATE_CONFLICT,
  413 RESOURCE_EXHAUSTED, 500 COMPUTATION_FAILURE.
- ``GET  /api/rulesets/{id}``      — fetch the stored spec + diagnostics.
- ``POST /api/rulesets/{id}/lex``  — lex text; tokens carry original offsets.
- ``GET  /api/runs/{run_id}``      — replay the structured log of one run.
- ``GET  /api/health``             — liveness.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import Body, FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import Settings
from .corpus.spec import RuleSetSpec
from .errors import AppError, ErrorCategory, computation_failure, input_error
from .index.db import Database
from .index.models import RuleSetRecord
from .kernel.compiler import CompiledLexer, compile_ruleset
from .kernel.lexer import lex
from .query.validation import parse_lex_request, parse_ruleset_request
from .runlog import RunLogger, new_run_id

Handler = Callable[[RunLogger], tuple[int, dict[str, Any]]]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = Database(settings.db_path)
    app = FastAPI(title="regex-lexer-backend", version="1.0.0")
    compiled_cache: dict[str, CompiledLexer] = {}

    def execute(op: str, handler: Handler) -> JSONResponse:
        """Run ``handler`` inside the run-log / error-taxonomy envelope."""
        run_id = new_run_id()
        logger = RunLogger(db, run_id, op)
        try:
            status, payload = handler(logger)
        except AppError as err:
            logger.log(
                "error",
                category=err.category.value,
                message=err.message,
                detail=err.detail,
            )
            return JSONResponse(
                status_code=err.http_status,
                content={"error": err.to_dict(), "run_id": run_id},
            )
        except Exception as exc:  # noqa: BLE001 - mapped to COMPUTATION_FAILURE
            failure = computation_failure(
                "internal computation failure", detail={"cause": repr(exc)}
            )
            logger.log(
                "error",
                category=failure.category.value,
                message=failure.message,
                detail=failure.detail,
            )
            return JSONResponse(
                status_code=failure.http_status,
                content={"error": failure.to_dict(), "run_id": run_id},
            )
        payload["run_id"] = run_id
        return JSONResponse(status_code=status, content=payload)

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_request: Any, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "category": ErrorCategory.INPUT_ERROR.value,
                    "message": "malformed request body",
                    "detail": {"errors": jsonable_encoder(exc.errors())},
                },
                "run_id": None,
            },
        )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/rulesets")
    def create_ruleset(body: Any = Body(default=None)) -> JSONResponse:
        def handler(logger: RunLogger) -> tuple[int, dict[str, Any]]:
            spec = parse_ruleset_request(body, settings)
            logger.log(
                "validate",
                name=spec.name,
                rules=len(spec.rules),
                newline_mode="LF",
                alphabet="U+0000..U+10FFFF",
            )
            compiled = compile_ruleset(spec, settings, logger)
            record = RuleSetRecord(
                id="rs_" + uuid.uuid4().hex[:16],
                name=spec.name,
                created_at=datetime.now(timezone.utc).isoformat(),
                spec=spec.model_dump(),
                diagnostics=compiled.diagnostics.to_dict(),
            )
            db.insert_ruleset(record)
            compiled_cache[record.id] = compiled
            logger.log("stored", ruleset_id=record.id)
            return 201, {
                "ruleset_id": record.id,
                "name": record.name,
                "diagnostics": record.diagnostics,
            }

        return execute("create_ruleset", handler)

    @app.get("/api/rulesets/{ruleset_id}")
    def get_ruleset(ruleset_id: str) -> JSONResponse:
        def handler(logger: RunLogger) -> tuple[int, dict[str, Any]]:
            record = db.get_ruleset(ruleset_id)
            if record is None:
                raise input_error(
                    f"unknown ruleset {ruleset_id!r}", http_status=404
                )
            logger.log("loaded", ruleset_id=ruleset_id)
            return 200, {
                "ruleset_id": record.id,
                "name": record.name,
                "created_at": record.created_at,
                "spec": record.spec,
                "diagnostics": record.diagnostics,
            }

        return execute("get_ruleset", handler)

    @app.post("/api/rulesets/{ruleset_id}/lex")
    def lex_text(ruleset_id: str, body: Any = Body(default=None)) -> JSONResponse:
        def handler(logger: RunLogger) -> tuple[int, dict[str, Any]]:
            record = db.get_ruleset(ruleset_id)
            if record is None:
                raise input_error(
                    f"unknown ruleset {ruleset_id!r}", http_status=404
                )
            text = parse_lex_request(body, settings)
            logger.log("request", ruleset_id=ruleset_id, text_length=len(text))
            compiled = compiled_cache.get(ruleset_id)
            if compiled is None:
                compiled = compile_ruleset(
                    RuleSetSpec.model_validate(record.spec), settings, logger
                )
                compiled_cache[ruleset_id] = compiled
            tokens, error = lex(compiled, text)
            logger.log(
                "lex",
                token_count=len(tokens),
                error=None if error is None else error.to_dict(),
            )
            return 200, {
                "tokens": [token.to_dict() for token in tokens],
                "error": None if error is None else error.to_dict(),
            }

        return execute("lex", handler)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> JSONResponse:
        def handler(_logger: RunLogger) -> tuple[int, dict[str, Any]]:
            entries = db.get_logs(run_id)
            if not entries:
                raise input_error(f"unknown run {run_id!r}", http_status=404)
            return 200, {
                "run_id": run_id,
                "entries": [entry.model_dump() for entry in entries],
            }

        return execute("get_run", handler)

    return app


app = create_app()
