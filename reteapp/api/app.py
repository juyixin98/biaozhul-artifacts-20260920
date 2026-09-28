"""FastAPI application factory and route handlers."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..config import Settings, load_settings
from ..core.errors import ReteError
from ..logging_setup import configure_logging
from ..service import SessionManager, UnknownSessionError
from ..version import __version__
from .schemas import CreateRunRequest, FireRequest, InsertFactRequest

# Category -> HTTP status. Categories are the engine's explicit failure
# taxonomy; unknown categories fall back to 400 rather than 200.
_ERROR_STATUS = {
    "fact_validation_error": 422,
    "rule_evaluation_error": 422,
    "unknown_fact_error": 404,
    "unknown_session_error": 404,
    "session_error": 400,
}


def create_app(
    settings: Settings | None = None,
    manager: SessionManager | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level)
    app = FastAPI(
        title="Rete Production Engine",
        version=__version__,
        description="Structured Rete matching network with evidence storage",
    )
    app.state.settings = settings
    app.state.sessions = manager or SessionManager()

    @app.middleware("http")
    async def correlate(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or f"req-{uuid.uuid4().hex[:12]}"
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Engine-Version"] = __version__
        return response

    @app.exception_handler(ReteError)
    async def rete_error_handler(_request: Request, exc: ReteError) -> JSONResponse:
        status = _ERROR_STATUS.get(exc.category, 400)
        return JSONResponse(
            status_code=status,
            content={
                "ok": False,
                "error": {"category": exc.category, "message": str(exc)},
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "error": {
                    "category": "request_validation_error",
                    "message": "request payload failed schema validation",
                    "details": exc.errors(),
                },
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_handler(request: Request, exc: Exception) -> JSONResponse:
        # Never fold an unknown failure into a success envelope.
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": {
                    "category": "internal_error",
                    "message": f"{type(exc).__name__}: {exc}",
                    "request_id": getattr(request.state, "request_id", None),
                },
            },
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/version")
    async def version() -> dict[str, Any]:
        return {"version": __version__, "settings": settings.describe()}

    @app.post("/runs", status_code=201)
    async def create_run(body: CreateRunRequest) -> dict[str, Any]:
        session = app.state.sessions.create(
            body.rules,
            settings,
            run_id=body.run_id,
            duplicate_policy=body.duplicate_policy,
        )
        return {
            "ok": True,
            "run_id": session.run_id,
            "version": __version__,
            "settings": settings.describe(),
            "rules": [r.name for r in session.engine.compiled_rules.values()],
            "duplicate_policy": body.duplicate_policy,
        }

    @app.get("/runs")
    async def list_runs() -> dict[str, Any]:
        return {"ok": True, "runs": app.state.sessions.list(), "version": __version__}

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        session = app.state.sessions.get(run_id)
        header = session.store.get_run(run_id)
        return {"ok": True, "run": header, "evidence": session.evidence()}

    @app.post("/runs/{run_id}/facts", status_code=201)
    async def insert_fact(run_id: str, body: InsertFactRequest) -> dict[str, Any]:
        session = app.state.sessions.get(run_id)
        result = session.insert(body.fact, duplicate_policy=body.duplicate_policy)
        return {
            "ok": True,
            "wme_id": result.wme_id,
            "accepted": result.accepted,
            "duplicate": result.duplicate,
            "activated_rules": result.activations,
            "agenda_size": len(session.engine.agenda),
        }

    @app.delete("/runs/{run_id}/facts/{wme_id}")
    async def retract_fact(run_id: str, wme_id: int) -> dict[str, Any]:
        session = app.state.sessions.get(run_id)
        removed = session.retract(wme_id)
        return {
            "ok": True,
            "retracted": removed,
            "agenda_size": len(session.engine.agenda),
        }

    @app.get("/runs/{run_id}/facts")
    async def list_facts(run_id: str) -> dict[str, Any]:
        session = app.state.sessions.get(run_id)
        return {"ok": True, "facts": session.working_memory()}

    @app.get("/runs/{run_id}/agenda")
    async def get_agenda(run_id: str) -> dict[str, Any]:
        session = app.state.sessions.get(run_id)
        return {"ok": True, "agenda": session.agenda(), "count": len(session.engine.agenda)}

    @app.post("/runs/{run_id}/fire")
    async def fire(run_id: str, body: FireRequest) -> dict[str, Any]:
        session = app.state.sessions.get(run_id)
        report = session.fire(body.mode, body.max_firings)
        payload = report.to_dict()
        # 200 even for limit_reached: firing ran and the bounded status is an
        # explicit, auditable outcome - but it is never reported as "ok match".
        payload["ok"] = True
        payload["bounded"] = True
        return payload

    @app.get("/runs/{run_id}/network")
    async def network(run_id: str) -> dict[str, Any]:
        session = app.state.sessions.get(run_id)
        return {"ok": True, "network": session.digest()}

    @app.get("/runs/{run_id}/trace")
    async def trace(run_id: str) -> dict[str, Any]:
        session = app.state.sessions.get(run_id)
        return {"ok": True, "run_id": run_id, "trace": session.trace()}

    @app.get("/runs/{run_id}/evidence")
    async def evidence(run_id: str) -> dict[str, Any]:
        session = app.state.sessions.get(run_id)
        return {"ok": True, "run_id": run_id, "evidence": session.evidence()}

    return app


def uvicorn_main() -> None:  # pragma: no cover - process entry point
    import uvicorn

    uvicorn.run(
        "reteapp.api.app:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
    )


app = create_app()
