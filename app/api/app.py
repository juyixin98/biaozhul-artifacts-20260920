"""Query interface: FastAPI application.

Every request gets a request id (response header ``X-Request-Id``); every
mutation is recorded in the evidence store together with a diagnostic
record stating why it was accepted or rejected, plus key engine state
(label sizes, nogood count, incomplete flag).
"""

from __future__ import annotations

import uuid
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..config import Budget, Settings, load_settings
from ..core.engine import ATMS
from ..diagnostics import redact_detail
from ..rules.schema import AssumptionSpec, PremiseSpec, RuleSpec
from ..store.db import EvidenceStore


class SessionCreate(BaseModel):
    session_id: str | None = Field(default=None, min_length=1, max_length=64)
    budget: dict | None = None


class SessionManager:
    """Owns live engines; rebuilds them from the operation log on demand."""

    def __init__(self, store: EvidenceStore, settings: Settings):
        self.store = store
        self.settings = settings
        self._engines: dict[str, ATMS] = {}

    def get(self, session_id: str) -> ATMS:
        if session_id in self._engines:
            return self._engines[session_id]
        if not self.store.session_exists(session_id):
            raise KeyError(session_id)
        engine = ATMS(self.settings.budget)
        for record in self.store.load_operations(session_id):
            self._replay(engine, record)
        self._engines[session_id] = engine
        return engine

    @staticmethod
    def _replay(engine: ATMS, record: dict) -> None:
        op, payload = record["op"], record["payload"]
        if op == "assume":
            engine.add_assumption(payload["name"])
        elif op == "retract":
            engine.retract_assumption(payload["name"])
        elif op == "premise":
            engine.add_premise(payload["node"])
        elif op == "rule":
            spec = RuleSpec(**payload)
            engine.add_rule(spec.to_rule())

    def create(self, session_id: str, budget: Budget) -> None:
        self.store.create_session(session_id, asdict(budget))
        self._engines[session_id] = ATMS(budget)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    store = EvidenceStore(settings.db_path)
    manager = SessionManager(store, settings)
    app = FastAPI(title="ATMS teaching backend", version="0.1.0")

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response

    def diagnose(
        request: Request,
        session_id: str | None,
        event: str,
        detail: dict,
        level: str = "info",
    ) -> None:
        store.append_diagnostic(
            request_id=request.state.request_id,
            session_id=session_id,
            level=level,
            event=event,
            detail=redact_detail(detail, settings.log_symbol_names),
        )

    def engine_or_404(session_id: str) -> ATMS:
        try:
            return manager.get(session_id)
        except KeyError:
            raise HTTPException(404, f"unknown session: {session_id}") from None

    def engine_state(engine: ATMS) -> dict:
        return {
            "assumptions": len(engine.assumptions),
            "rules": len(engine.rules),
            "nogoods": len(engine.nogoods),
            "labelled_nodes": len(engine.labels),
            "incomplete": engine.incomplete,
        }

    # -------------------------------------------------------------- #
    # routes
    # -------------------------------------------------------------- #

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/sessions", status_code=201)
    def create_session(body: SessionCreate, request: Request) -> dict:
        session_id = body.session_id or uuid.uuid4().hex[:12]
        if store.session_exists(session_id):
            diagnose(request, session_id, "session-rejected",
                     {"reason": "session-id-exists"}, level="warning")
            raise HTTPException(409, f"session exists: {session_id}")
        budget = settings.budget
        if body.budget:
            budget = Budget(**{**asdict(settings.budget), **body.budget})
        manager.create(session_id, budget)
        diagnose(request, session_id, "session-created",
                 {"budget": asdict(budget)})
        return {"session_id": session_id, "budget": asdict(budget)}

    @app.post("/sessions/{session_id}/assumptions", status_code=201)
    def add_assumption(session_id: str, body: AssumptionSpec, request: Request):
        engine = engine_or_404(session_id)
        result = engine.add_assumption(body.name)
        diagnose(request, session_id, "assumption",
                 {"accepted": result.accepted, "reason": result.reason,
                  **result.detail, "state": engine_state(engine)},
                 level="info" if result.accepted else "warning")
        if not result.accepted:
            return JSONResponse(
                status_code=409,
                content={"accepted": False, "reason": result.reason},
            )
        store.append_operation(session_id, request.state.request_id,
                               "assume", {"name": body.name})
        return {"accepted": True, "reason": result.reason}

    @app.delete("/sessions/{session_id}/assumptions/{name}")
    def retract_assumption(session_id: str, name: str, request: Request):
        engine = engine_or_404(session_id)
        result = engine.retract_assumption(name)
        diagnose(request, session_id, "retraction",
                 {"accepted": result.accepted, "reason": result.reason,
                  **result.detail, "state": engine_state(engine)},
                 level="info" if result.accepted else "warning")
        if not result.accepted:
            raise HTTPException(404, result.reason)
        store.append_operation(session_id, request.state.request_id,
                               "retract", {"name": name})
        return {"accepted": True, "reason": result.reason}

    @app.post("/sessions/{session_id}/premises", status_code=201)
    def add_premise(session_id: str, body: PremiseSpec, request: Request):
        engine = engine_or_404(session_id)
        result = engine.add_premise(body.node)
        diagnose(request, session_id, "premise",
                 {"accepted": result.accepted, "reason": result.reason,
                  **result.detail, "state": engine_state(engine)},
                 level="info" if result.accepted else "warning")
        if not result.accepted:
            return JSONResponse(
                status_code=409,
                content={"accepted": False, "reason": result.reason},
            )
        store.append_operation(session_id, request.state.request_id,
                               "premise", {"node": body.node})
        return {"accepted": True, "reason": result.reason}

    @app.post("/sessions/{session_id}/rules", status_code=201)
    def add_rule(session_id: str, body: RuleSpec, request: Request):
        engine = engine_or_404(session_id)
        result = engine.add_rule(body.to_rule())
        diagnose(request, session_id, "rule",
                 {"accepted": result.accepted, "reason": result.reason,
                  "rule_id": body.rule_id, "state": engine_state(engine)},
                 level="info" if result.accepted else "warning")
        if not result.accepted:
            return JSONResponse(
                status_code=409,
                content={"accepted": False, "reason": result.reason},
            )
        store.append_operation(session_id, request.state.request_id,
                               "rule", body.model_dump())
        return {"accepted": True, "reason": result.reason}

    @app.get("/sessions/{session_id}/nodes/{node}/label")
    def get_label(session_id: str, node: str, request: Request) -> dict:
        engine = engine_or_404(session_id)
        result = engine.query(node)
        diagnose(request, session_id, "query",
                 {"node": node, "status": result["status"],
                  "env_count": len(result["environments"]),
                  "complete": result["complete"]})
        return result

    @app.get("/sessions/{session_id}/nogoods")
    def get_nogoods(session_id: str) -> dict:
        engine = engine_or_404(session_id)
        return {"nogoods": engine.known_nogoods(), "complete": not engine.incomplete}

    @app.get("/sessions/{session_id}/diagnostics")
    def get_diagnostics(session_id: str) -> dict:
        if not store.session_exists(session_id):
            raise HTTPException(404, f"unknown session: {session_id}")
        return {"diagnostics": store.load_diagnostics(session_id)}

    return app
