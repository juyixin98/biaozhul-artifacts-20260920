"""HTTP query interface (FastAPI).

Endpoints
---------
* ``POST /v1/snapshots``          load a versioned synthetic input snapshot
* ``POST /v1/provenance/query``   answer a query with symbolic provenance;
                                  optionally run the independent numeric-weight
                                  verification in the same call
* ``GET  /v1/runs/{request_id}``  read back an answer and its provenance from
                                  the committed input version
* ``GET  /healthz``               liveness

The layer only (de)serialises and maps typed domain errors to HTTP statuses;
all semantics live in the planner/engine. Every error carries the stable
failure ``category`` so clients and tests can assert the failure class.
"""
from __future__ import annotations

import uuid
from fractions import Fraction
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .diagnostics import JsonDiagnostics
from .errors import (
    InputVersionError,
    PlanError,
    ProvenanceError,
    SnapshotError,
    TypeRuleError,
    WeightError,
)
from .evidence_store import EvidenceStore, Snapshot
from .planner import Planner
from .rule_language import parse_plan
from .weight_check import coerce_weights, cross_check, evaluate_numeric

_STATUS_BY_CATEGORY = {
    "rejected_plan": 400,
    "rejected_weights": 400,
    "rejected_snapshot": 400,
    "rejected_input_version": 422,
    "indeterminate_type": 422,
}


def create_app(settings: Settings, *, diagnostics: JsonDiagnostics | None = None) -> FastAPI:
    app = FastAPI(
        title="Symbolic Provenance Service",
        version="1.0.0",
        description="Positive relational queries (select/project/join/union) with N[X] provenance.",
    )
    app.state.settings = settings
    app.state.store = EvidenceStore(settings.db_path)
    app.state.diagnostics = diagnostics or JsonDiagnostics()

    @app.exception_handler(ProvenanceError)
    async def _handle_domain_error(_: Request, exc: ProvenanceError) -> JSONResponse:
        request_id = uuid.uuid4().hex
        status = _STATUS_BY_CATEGORY.get(exc.category, 422)
        app.state.diagnostics.rejected(
            exc.message,
            {"category": exc.category, "details": exc.details},
            request_id=request_id,
        )
        return JSONResponse(
            status_code=status,
            content={
                "ok": False,
                "request_id": request_id,
                "category": exc.category,
                "error": exc.message,
                "details": exc.details,
            },
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/snapshots")
    async def load_snapshot(body: dict[str, Any]) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        replace = bool(body.get("replace", False))
        snapshot = Snapshot.from_dict(body.get("snapshot"))
        hashes = app.state.store.load_snapshot(snapshot, replace=replace)
        app.state.diagnostics.accepted(
            "snapshot loaded",
            {"version": snapshot.version, "relations": list(hashes)},
            request_id=request_id,
        )
        return {
            "ok": True,
            "request_id": request_id,
            "version": snapshot.version,
            "content_hashes": hashes,
        }

    @app.post("/v1/provenance/query")
    async def query(body: dict[str, Any]) -> dict[str, Any]:
        request_id = body.get("request_id") or uuid.uuid4().hex
        raw_plan = body.get("plan")
        if raw_plan is None:
            raise PlanError("request body must contain a 'plan' object")

        planner = Planner(app.state.store, app.state.diagnostics)
        result = planner.run(raw_plan, request_id=request_id)
        payload = result.to_payload()

        weights = body.get("weights")
        if weights is not None:
            numeric_weights = coerce_weights(weights)
            numeric = evaluate_numeric(parse_plan(raw_plan), app.state.store, numeric_weights, result.version)
            mismatches = cross_check(result.rows, numeric, numeric_weights)
            payload["weight_verification"] = {
                "agreed": not mismatches,
                "checked_outputs": len(result.rows),
                "mismatches": [
                    {
                        "output": m.output,
                        "symbolic": _frac(m.symbolic),
                        "numeric": None if m.numeric is None else _frac(m.numeric),
                    }
                    for m in mismatches
                ],
            }
        return {"ok": True, **payload}

    @app.get("/v1/runs/{request_id}")
    async def read_run(request_id: str) -> dict[str, Any]:
        record = app.state.store.read_back(request_id)
        if record is None:
            raise InputVersionError(
                f"no stored run {request_id!r}", details={"request_id": request_id}
            )
        return {"ok": True, **record}

    return app


def _frac(value: Fraction) -> str:
    # Stable exact rendering, e.g. "1/3" or "2".
    return str(value)


# Default app used by ``uvicorn provenance.api:app``, configured from env.
app = create_app(Settings.from_env())
